from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import requests
import websockets


DEBUG_LIST_ENDPOINT = "http://127.0.0.1:9222/json/list"
VBK_ORDER_LIST_URL = "https://vbooking.ctrip.com/order/orderList"
VBK_HOLD_LIST_URL = "https://vbooking.ctrip.com/order/holdOrderList"


class VbkBridgeError(RuntimeError):
    pass


@dataclass
class VbkOrderItem:
    order_no: str
    product_id: str = ""
    product_name: str = ""
    supplier_product_name: str = ""
    order_type_text: str = ""
    confirm_status_text: str = ""
    payment_status_text: str = ""
    departure_date: str = ""
    departure_city: str = ""
    customer_name: str = ""
    customer_phone_masked: str = ""
    distribution_channel: str = ""
    pending_type_text: str = ""
    room_nights_text: str = ""
    adult_count: int = 1
    child_count: int = 0
    raw: dict[str, Any] | None = None


class ChromeVbkBridge:
    def _get_page_target(self, list_kind: str = "order") -> dict[str, Any]:
        target_url = VBK_ORDER_LIST_URL if list_kind == "order" else VBK_HOLD_LIST_URL
        try:
            with urllib.request.urlopen(DEBUG_LIST_ENDPOINT, timeout=5) as resp:
                targets = json.load(resp)
        except Exception as e:
            raise VbkBridgeError("未检测到 9222 调试浏览器，请先启动带 remote-debugging 的 Chrome") from e

        pages = [t for t in targets if t.get("type") == "page"]
        for target in pages:
            if str(target.get("url", "")).startswith(target_url):
                return target
        fallback_urls = {VBK_ORDER_LIST_URL, VBK_HOLD_LIST_URL}
        for target in pages:
            url = str(target.get("url", ""))
            if any(url.startswith(prefix) for prefix in fallback_urls):
                return target
        raise VbkBridgeError(f"未检测到已打开的 VBK 列表页：{target_url}")

    async def _call(self, conn, state: dict[str, int], method: str, params: dict[str, Any] | None = None, timeout: int = 20):
        state["id"] += 1
        mid = state["id"]
        await conn.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(conn.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                return msg

    async def _runtime_eval(self, expression: str, list_kind: str = "order", return_by_value: bool = True, timeout: int = 20) -> Any:
        target = self._get_page_target(list_kind=list_kind)
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise VbkBridgeError("未获取到 VBK 页面 websocket 地址")

        async with websockets.connect(ws_url, max_size=20_000_000) as conn:
            state = {"id": 0}
            await self._call(conn, state, "Page.enable")
            await self._call(conn, state, "Runtime.enable")
            res = await self._call(
                conn,
                state,
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": return_by_value, "awaitPromise": True},
                timeout=timeout,
            )
            if "exceptionDetails" in res.get("result", {}):
                text = res["result"]["exceptionDetails"].get("text") or "页面执行失败"
                raise VbkBridgeError(text)
            result = res.get("result", {}).get("result", {})
            return result.get("value") if return_by_value else result

    def eval(self, expression: str, list_kind: str = "order", return_by_value: bool = True, timeout: int = 20) -> Any:
        return asyncio.run(self._runtime_eval(expression, list_kind=list_kind, return_by_value=return_by_value, timeout=timeout))

    def extract_visible_orders(self, list_kind: str = "order") -> list[VbkOrderItem]:
        expr = r'''(() => {
          const bodyText = document.body ? document.body.innerText : '';
          const blocks = bodyText.split(/订单号：/).slice(1);
          const rows = [];
          for (const block of blocks) {
            const lines = block.split('\n').map(x => x.trim()).filter(Boolean);
            if (!lines.length) continue;
            const first = lines[0] || '';
            const orderNo = (first.match(/^\d{10,20}/) || [])[0] || '';
            if (!orderNo) continue;
            const whole = lines.join('\n');
            const get = (re) => {
              const m = whole.match(re);
              return m ? (m[1] || '').trim() : '';
            };
            const customerLine = lines.find(x => /真实号码/.test(x)) || '';
            const customerNameIdx = customerLine ? lines.indexOf(customerLine) - 1 : -1;
            const customerName = customerNameIdx >= 0 ? (lines[customerNameIdx] || '') : '';
            const countText = lines.find(x => /\d+成人\d+儿童/.test(x)) || '';
            const countMatch = countText.match(/(\d+)成人(\d+)儿童/);
            rows.push({
              order_no: orderNo,
              product_id: get(/产品ID：([0-9]+)/),
              product_name: get(/产品名称：([^\n]+)/),
              supplier_product_name: get(/供应商产品名称：([^\n]+)/),
              confirm_status_text: (() => {
                const candidates = ['新订待处理','修改待处理','取消待处理','已确认','已取消','已拒绝'];
                return candidates.find(x => whole.includes(x)) || '';
              })(),
              payment_status_text: (() => {
                const candidates = ['未支付','部分支付','已支付','已退款'];
                return candidates.find(x => whole.includes(x)) || '';
              })(),
              departure_date: (whole.match(/\b20\d{2}-\d{2}-\d{2}\b/) || [''])[0],
              departure_city: (() => {
                const line = lines.find(x => /出发$|出发$/.test(x) || /出发/.test(x));
                return line && /出发/.test(line) ? line : '';
              })(),
              customer_name: customerName,
              customer_phone_masked: get(/\(真实号码([^\n]*)\)/),
              distribution_channel: (() => {
                const channels = ['携程门店','百事通门店','携程用户','去哪儿','同程'];
                return channels.find(x => whole.includes(x)) || '';
              })(),
              order_type_text: (() => {
                const types = ['标准单','占位单'];
                return types.find(x => whole.includes(x)) || '';
              })(),
              pending_type_text: (() => {
                const types = ['新订待处理','修改待处理','取消待处理'];
                return types.find(x => whole.includes(x)) || '';
              })(),
              room_nights_text: lines.find(x => /套餐/.test(x) || /成人\d+儿童/.test(x)) || '',
              adult_count: countMatch ? Number(countMatch[1]) : 1,
              child_count: countMatch ? Number(countMatch[2]) : 0,
              raw_text: whole.slice(0, 4000),
            });
          }
          return rows;
        })()'''
        data = self.eval(expr, list_kind=list_kind, timeout=30)
        if not isinstance(data, list):
            raise VbkBridgeError("未能从 VBK 页面提取订单列表")
        results: list[VbkOrderItem] = []
        for row in data:
            results.append(
                VbkOrderItem(
                    order_no=str(row.get("order_no") or ""),
                    product_id=str(row.get("product_id") or ""),
                    product_name=str(row.get("product_name") or ""),
                    supplier_product_name=str(row.get("supplier_product_name") or ""),
                    order_type_text=str(row.get("order_type_text") or ""),
                    confirm_status_text=str(row.get("confirm_status_text") or ""),
                    payment_status_text=str(row.get("payment_status_text") or ""),
                    departure_date=str(row.get("departure_date") or ""),
                    departure_city=str(row.get("departure_city") or ""),
                    customer_name=str(row.get("customer_name") or ""),
                    customer_phone_masked=str(row.get("customer_phone_masked") or ""),
                    distribution_channel=str(row.get("distribution_channel") or ""),
                    pending_type_text=str(row.get("pending_type_text") or ""),
                    room_nights_text=str(row.get("room_nights_text") or ""),
                    adult_count=int(row.get("adult_count") or 1),
                    child_count=int(row.get("child_count") or 0),
                    raw=row,
                )
            )
        return results

    def search_order_by_order_no(self, order_no: str, list_kind: str = "order") -> VbkOrderItem | None:
        order_no = (order_no or "").strip()
        if not order_no:
            raise ValueError("订单号不能为空")
        expr = r'''async (targetOrderNo) => {
          const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
          const input = document.querySelector('#all_oid');
          if (!input) {
            throw new Error('未找到订单号输入框 #all_oid');
          }
          const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
          if (nativeSetter) {
            nativeSetter.call(input, targetOrderNo);
          } else {
            input.value = targetOrderNo;
          }
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
          input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
          input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true }));

          const queryBtn = [...document.querySelectorAll('button')].find(el => /查\s*询/.test((el.innerText || el.textContent || '').replace(/\s+/g, '')));
          if (!queryBtn) {
            throw new Error('未找到查询按钮');
          }
          queryBtn.click();

          const start = Date.now();
          while (Date.now() - start < 15000) {
            await sleep(400);
            const text = document.body ? document.body.innerText : '';
            if (text.includes(`订单号：${targetOrderNo}`)) {
              return { ok: true, found: true, bodyText: text.slice(0, 120000) };
            }
            if (/共\s*0\s*条记录/.test(text) || /暂无数据/.test(text)) {
              return { ok: true, found: false, bodyText: text.slice(0, 120000) };
            }
          }
          return { ok: false, found: false, message: '查询超时', bodyText: (document.body ? document.body.innerText : '').slice(0, 120000) };
        }'''
        result = self.eval(f"({expr})({json.dumps(order_no, ensure_ascii=False)})", list_kind=list_kind, timeout=40)
        if not isinstance(result, dict):
            raise VbkBridgeError("VBK 订单号查询返回异常")
        if not result.get("ok"):
            raise VbkBridgeError(result.get("message") or "VBK 查询失败")
        if not result.get("found"):
            return None
        rows = self.extract_visible_orders(list_kind=list_kind)
        for item in rows:
            if item.order_no == order_no:
                return item
        return None


def normalize_order_status(item: VbkOrderItem) -> str:
    text = item.confirm_status_text or item.pending_type_text or ""
    if text in {"新订待处理", "修改待处理", "取消待处理", "待处理"}:
        return "待确认"
    if text == "已确认":
        return "已确认"
    if text in {"已取消", "已拒绝"}:
        return "已取消"
    return text or "待确认"


def normalize_payment_status(item: VbkOrderItem) -> str:
    mapping = {
        "未支付": "未支付",
        "部分支付": "部分支付",
        "已支付": "已支付",
        "已退款": "已退款",
    }
    return mapping.get(item.payment_status_text or "", item.payment_status_text or "未支付")


def build_order_payload_from_vbk(item: VbkOrderItem) -> dict[str, Any]:
    route_name = item.supplier_product_name or item.product_name
    initial_note_parts = [
        f"来源: VBK 页面抓取",
        f"VBK类型: {item.order_type_text or '-'}",
        f"确认状态: {item.confirm_status_text or item.pending_type_text or '-'}",
        f"分销渠道: {item.distribution_channel or '-'}",
    ]
    return {
        "order_no": item.order_no,
        "external_order_no": item.order_no,
        "product_name": item.product_name or route_name or item.order_no,
        "route_name": route_name,
        "channel": item.distribution_channel or "携程",
        "source_platform": "VBK",
        "customer_name": item.customer_name or "携程用户",
        "customer_phone": item.customer_phone_masked,
        "backup_contact": "",
        "customer_note": "",
        "departure_date": item.departure_date or "1970-01-01",
        "return_date": "",
        "adult_count": item.adult_count or 1,
        "child_count": item.child_count or 0,
        "room_count": None,
        "total_amount": None,
        "paid_amount": 0,
        "currency": "CNY",
        "payment_status": normalize_payment_status(item),
        "order_status": normalize_order_status(item),
        "follow_status": "待跟进",
        "priority": "普通",
        "owner_id": 1,
        "next_follow_up_at": "",
        "initial_note": "；".join(initial_note_parts),
    }
