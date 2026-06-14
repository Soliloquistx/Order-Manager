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
    def _cdp_request(self, path: str, params: dict[str, Any] | None = None, timeout: int = 10, http_method: str | None = None) -> Any:
        """Send a CDP HTTP request to /json endpoints."""
        url = f"http://127.0.0.1:9222{path}"
        data = json.dumps(params or {}).encode() if params else None
        req = urllib.request.Request(url, data=data, method=http_method or ('POST' if params else 'GET'),
                                      headers={'Content-Type': 'application/json'} if params else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as e:
            raise VbkBridgeError(f"CDP 请求失败: {path} {e}") from e

    def _open_detail_page(self, order_no: str, list_kind: str = "order") -> str:
        item = self.search_order_by_order_no(order_no, list_kind=list_kind)
        if not item:
            raise VbkBridgeError(f"未在 VBK {list_kind} 列表中找到订单号 {order_no}")
        target_url = (
            f"https://vbooking.ctrip.com/order/orderDetail?orderId={order_no}"
            if list_kind == "order"
            else f"https://vbooking.ctrip.com/order/holdOrderDetail?orderId={order_no}"
        )
        created = self._cdp_request("/json/new?" + urllib.parse.quote(target_url, safe=':/?=&'), http_method='PUT', timeout=15)
        ws_url = created.get("webSocketDebuggerUrl") if isinstance(created, dict) else None
        if not ws_url:
            raise VbkBridgeError("打开 VBK 详情页失败")
        return ws_url

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

        # 没有已打开的列表页 → 自动创建（独立线程避免 asyncio 冲突）
        try:
            import threading, time

            result_holder = {}

            def _auto_open_thread():
                loop = None
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    browser_ws = json.loads(
                        urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=5).read()
                    ).get("webSocketDebuggerUrl", "")
                    if not browser_ws:
                        return

                    async def _do():
                        async with websockets.connect(browser_ws, max_size=2_000_000) as conn:
                            await conn.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                                         "params": {"url": target_url}}))
                            msg = await asyncio.wait_for(conn.recv(), timeout=20)
                            new_id = json.loads(msg).get("result", {}).get("targetId", "")
                        if not new_id:
                            return
                        for _ in range(15):
                            await asyncio.sleep(1)
                            with urllib.request.urlopen(DEBUG_LIST_ENDPOINT, timeout=5) as resp:
                                targets = json.load(resp)
                            for t in targets:
                                if t.get("id") == new_id and t.get("type") == "page":
                                    await asyncio.sleep(5)
                                    result_holder["target"] = t
                                    return

                    loop.run_until_complete(_do())
                finally:
                    loop.close()

            t = threading.Thread(target=_auto_open_thread)
            t.start()
            t.join(timeout=35)
            if result_holder.get("target"):
                return result_holder["target"]
        except Exception:
            pass
        raise VbkBridgeError(f"未检测到已打开的 VBK 列表页：{target_url}，自动打开失败（请确保浏览器已登录 VBK）")

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
          // 等页面完全加载
          for (let i = 0; i < 20; i++) {
            const input = document.querySelector('#all_oid');
            if (input) break;
            await sleep(1500);
          }
          const input = document.querySelector('#all_oid');
          if (!input) {
            throw new Error('未找到订单号输入框 #all_oid（页面加载超时）');
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

    def extract_detail_by_order_no(self, order_no: str, list_kind: str = "order") -> dict[str, Any]:
        order_no = (order_no or "").strip()
        if not order_no:
            raise ValueError("订单号不能为空")

        detail_target = None
        try:
            with urllib.request.urlopen(DEBUG_LIST_ENDPOINT, timeout=5) as resp:
                targets = json.load(resp)
            expected = (
                f"https://vbooking.ctrip.com/order/orderDetail?orderId={order_no}"
                if list_kind == "order"
                else f"https://vbooking.ctrip.com/order/holdOrderDetail?orderId={order_no}"
            )
            for t in targets:
                if t.get("type") == "page" and str(t.get("url", "")).startswith(expected):
                    detail_target = t
                    break
        except Exception:
            detail_target = None

        if detail_target and detail_target.get("webSocketDebuggerUrl"):
            ws_url = detail_target["webSocketDebuggerUrl"]
        else:
            ws_url = self._open_detail_page(order_no, list_kind=list_kind)

        async def _run():
            async with websockets.connect(ws_url, max_size=20_000_000) as conn:
                state = {"id": 0}
                await self._call(conn, state, "Page.enable")
                await self._call(conn, state, "Runtime.enable")
                await self._call(
                    conn,
                    state,
                    "Runtime.evaluate",
                    {"expression": "new Promise(r => setTimeout(r, 3000))", "returnByValue": True, "awaitPromise": True},
                    timeout=20,
                )

                pre = await self._call(
                    conn,
                    state,
                    "Runtime.evaluate",
                    {
                        "expression": "({title: document.title, bodyText: document.body ? document.body.innerText : '', tableTexts: [...document.querySelectorAll('table')].map(t => t.innerText || '')})",
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                    timeout=60,
                )
                pre_value = pre.get("result", {}).get("result", {}).get("value") or {}

                await self._call(
                    conn,
                    state,
                    "Runtime.evaluate",
                    {
                        "expression": "(() => { const btn = document.evaluate(\"//*[contains(text(),'查看加密信息')]\", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue; if (btn) { btn.click(); return {clicked:true}; } return {clicked:false}; })()",
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                    timeout=30,
                )
                await self._call(
                    conn,
                    state,
                    "Runtime.evaluate",
                    {"expression": "new Promise(r => setTimeout(r, 2500))", "returnByValue": True, "awaitPromise": True},
                    timeout=20,
                )

                post = await self._call(
                    conn,
                    state,
                    "Runtime.evaluate",
                    {
                        "expression": "({bodyText: document.body ? document.body.innerText : '', tableTexts: [...document.querySelectorAll('table')].map(t => t.innerText || '')})",
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                    timeout=60,
                )
                post_value = post.get("result", {}).get("result", {}).get("value") or {}
                return pre_value, post_value

        pre_value, post_value = asyncio.run(_run())
        body_text = str(post_value.get("bodyText") or pre_value.get("bodyText") or "")
        table_texts = post_value.get("tableTexts") or pre_value.get("tableTexts") or []
        if not body_text:
            raise VbkBridgeError("VBK 详情页正文为空")

        detail = parse_vbk_detail_from_text(order_no, list_kind, body_text, table_texts)
        if not isinstance(detail, dict) or not detail.get("order_no"):
            raise VbkBridgeError("VBK 详情提取返回异常")
        return detail


def parse_vbk_detail_from_text(order_no: str, list_kind: str, body_text: str, table_texts: list[str]) -> dict[str, Any]:
    body_text = body_text or ""
    lines = [x.strip() for x in body_text.split("\n") if x.strip()]

    def get(pattern: str) -> str:
        import re
        m = re.search(pattern, body_text)
        return (m.group(1).strip() if m else "")

    def parse_date_pair() -> tuple[str, str]:
        import re
        m = re.search(r"(?:出发/返回日期|出发-返回日期)[:：]?\s*(20\d{2}-\d{2}-\d{2})\s*[/\-]\s*(20\d{2}-\d{2}-\d{2})", body_text)
        if not m:
            return "", ""
        return m.group(1), m.group(2)

    def parse_travellers() -> list[dict[str, Any]]:
        import re
        out: list[dict[str, Any]] = []
        target = ""
        for t in table_texts:
            if "证件类型" in t and "证件号" in t and "姓名" in t:
                target = t
                break
        if not target:
            return out

        text = target.replace("\t", "\n")
        m = re.search(r"姓名.*?操作\n(.*)$", text, re.S)
        if not m:
            return out
        data_text = m.group(1).strip()
        chunks = [c.strip() for c in re.split(r"\n(?=[\u4e00-\u9fa5A-Za-z·]{2,}\n60岁以上老人)", data_text) if c.strip()]
        for chunk in chunks:
            person = _parse_traveller_block([x.strip() for x in chunk.split("\n") if x.strip()])
            if person.get("name"):
                out.append(person)
        return out

    def _parse_traveller_block(block: list[str]) -> dict[str, Any]:
        import re
        text = "\n".join(block)
        name = block[0] if block else ""
        gender = "女" if "\n女\n" in f"\n{text}\n" else ("男" if "\n男\n" in f"\n{text}\n" else "")
        birth = ""
        m_birth = re.search(r"(19\d{2}-\d{2}-\d{2}|20\d{2}-\d{2}-\d{2})", text)
        if m_birth:
            birth = m_birth.group(1)
        id_no = ""
        m_id = re.search(r"身份证\s*[•·]\s*([0-9Xx]{15,18})", text)
        if m_id:
            id_no = m_id.group(1)
        phone = ""
        m_phone = re.search(r"(1\d{10}|0\d{2,3}\d+转\d+)", text)
        if m_phone:
            phone = m_phone.group(1)
        return {
            "name": name,
            "gender": gender,
            "birth_date": birth,
            "id_no": id_no,
            "id_type": "身份证",
            "person_type": "成人" if "成人" in text else "",
            "phone": phone,
            "encrypted_info_revealed": bool(birth or id_no),
            "from_vbk_detail": True,
        }

    def parse_flights() -> list[dict[str, Any]]:
        import re
        out: list[dict[str, Any]] = []
        target = ""
        for t in table_texts:
            if "航班号" in t and "起飞时间" in t:
                target = t
                break
        if not target:
            return out
        lines2 = [x.strip() for x in target.split("\n") if x.strip()]
        for i, line in enumerate(lines2):
            if re.fullmatch(r"[A-Z]{2}\d{3,5}", line):
                prev2 = lines2[i-2] if i >= 2 else ""
                prev1 = lines2[i-1] if i >= 1 else ""
                out.append({
                    "route": prev2,
                    "depart_time": prev1 if re.search(r"20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", prev1) else "",
                    "flight_no": line,
                })
        return out

    departure_date, return_date = parse_date_pair()
    travellers = parse_travellers()
    flights = parse_flights()
    pickup_dropoff = []
    if flights:
        pickup_dropoff.append({
            "action": "1接机/站",
            "date": departure_date,
            "location": "中川机场" if "中川机场" in body_text else "",
            "flight_no": flights[0].get("flight_no", ""),
            "time": flights[0].get("depart_time", "")[-8:-3] if flights[0].get("depart_time") else "",
            "description": "",
            "enabled": True,
        })
    if len(flights) > 1:
        pickup_dropoff.append({
            "action": "2送机/站",
            "date": return_date,
            "location": "中川机场" if "中川机场" in body_text else "",
            "flight_no": flights[1].get("flight_no", ""),
            "time": flights[1].get("depart_time", "")[-8:-3] if flights[1].get("depart_time") else "",
            "description": "",
            "enabled": True,
        })

    return {
        "order_no": order_no,
        "list_kind": list_kind,
        "order_type_text": "占位单" if list_kind == "hold" else "普通订单",
        "confirm_status_text": "已确认" if "已确认" in body_text else "",
        "payment_status_text": "已支付" if "已付款" in body_text or "已支付" in body_text else "",
        "departure_date": departure_date,
        "return_date": return_date,
        "departure_city": get(r"出发/定位城市[:：]?\s*([^\n]+)") or get(r"出发城市[:：]?\s*([^\n]+)"),
        "customer_name": get(r"(?:联系人|占位联系人)[:：]?\s*([^\n]+)"),
        "customer_phone": get(r"手机\s*([^\n]+)"),
        "distribution_channel": get(r"(?:分销渠道|占位单渠道)[:：]?\s*([^\n]+)"),
        "scenic_booking_no": get(r"订单编号[:：]?\s*([^\n]+)"),
        "reservation_scenic_name": "无需预约" if "无需预约" in body_text else "",
        "merchant_note": get(r"商家备注[:：]?\s*([^\n]+)"),
        "travellers": travellers,
        "pickup_dropoff": pickup_dropoff,
        "flights": flights,
        "raw_json": json.dumps({"bodyText": body_text[:120000], "tableTexts": table_texts[:8]}, ensure_ascii=False),
    }


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
        "initial_note": "",
    }
