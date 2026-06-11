from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass
from typing import Any

import requests
import websockets


DEBUG_ENDPOINT = "http://127.0.0.1:9222/json/list"
TARGET_URL_PREFIX = "https://t16.et818.com/XWJ/SysMain"
DETAIL_URL_TEMPLATE = "/XWJ/PlanReg/View/PlanMng.ListRegDSN1?pa=3&PlanID={plan_id}&BizMode={biz_mode}&FixedPlanClass=&FixedTourType=&RegID={reg_id}"
EDIT_URL_TEMPLATE = "/XWJ/PlanReg/AddDSN/PlanMng.ListRegDSN1?pa=3&PlanID={plan_id}&BizMode={biz_mode}&FixedPlanClass=&FixedTourType=&RegID={reg_id}&isRegDelete="
SEARCH_ENDPOINT = "https://t16.et818.com/XWJ/PlanReg/SearchReg/PlanMng.ListRegDSN1"


class Et818BridgeError(RuntimeError):
    pass


@dataclass
class Et818OrderMatch:
    order_no: str
    reg_id: int
    biz_mode: int | None = None
    plan_id: int | None = 0
    line_name: str = ""
    guest_summary: str = ""
    departure_date: str = ""
    phone: str = ""
    raw: dict[str, Any] | None = None


class ChromeEt818Bridge:
    def _get_target(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(DEBUG_ENDPOINT, timeout=5) as resp:
                targets = json.load(resp)
        except Exception as e:
            raise Et818BridgeError("未检测到 9222 调试浏览器，请先启动调试模式 Chrome/Edge") from e

        for target in targets:
            if target.get("type") == "page" and str(target.get("url", "")).startswith(TARGET_URL_PREFIX):
                return target
        raise Et818BridgeError("未检测到 ET818 已登录 SysMain 页面，请先在调试浏览器中登录 ET818")

    async def _call(self, conn, state: dict[str, int], method: str, params: dict[str, Any] | None = None, timeout: int = 20):
        state["id"] += 1
        mid = state["id"]
        await conn.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(conn.recv(), timeout=timeout)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                return msg

    async def _runtime_eval(self, expression: str, return_by_value: bool = True, timeout: int = 20) -> Any:
        target = self._get_target()
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise Et818BridgeError("未获取到调试浏览器 websocket 地址")

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
                raise Et818BridgeError(text)
            result = res.get("result", {}).get("result", {})
            return result.get("value") if return_by_value else result

    def eval(self, expression: str, return_by_value: bool = True, timeout: int = 20) -> Any:
        return asyncio.run(self._runtime_eval(expression, return_by_value=return_by_value, timeout=timeout))

    def ensure_sysmain(self) -> None:
        self._get_target()

    def get_cookies(self) -> dict[str, str]:
        target = self._get_target()
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise Et818BridgeError("未获取到调试浏览器 websocket 地址")

        async def _get():
            async with websockets.connect(ws_url, max_size=20_000_000) as conn:
                state = {"id": 0}
                await self._call(conn, state, "Network.enable")
                res = await self._call(conn, state, "Network.getAllCookies", {}, timeout=20)
                cookies = res.get("result", {}).get("cookies", [])
                return {
                    c["name"]: c["value"]
                    for c in cookies
                    if c.get("domain") == "t16.et818.com"
                }

        cookie_map = asyncio.run(_get())
        if "ASP.NET_SessionId" not in cookie_map:
            raise Et818BridgeError("未从调试浏览器中取到 ET818 登录会话 Cookie")
        return cookie_map

    def get_live_query_template(self) -> dict[str, Any]:
        expr = r'''(() => {
          const frame = [...document.querySelectorAll('iframe,frame')].find(f => (f.getAttribute('src') || '').includes('PlanReg/ListRegDSEx/PlanMng.ListRegDSN1'));
          if (!frame) return { ok: false, error: '未找到 ET818 未并团页，请先打开未并团' };
          const w = frame.contentWindow;
          const vTool = w.vTool || w.$vTool;
          if (!vTool) return { ok: false, error: '未找到 ET818 查询上下文 vTool' };
          return { ok: true, query: JSON.parse(JSON.stringify(vTool.query || {})) };
        })()'''
        data = self.eval(expr, timeout=20)
        if not isinstance(data, dict) or not data.get("ok"):
            raise Et818BridgeError((data or {}).get("error") or "未能读取 ET818 当前查询模板")
        return data.get("query") or {}

    def _search_via_requests(self, order_no: str) -> dict[str, Any]:
        cookies = self.get_cookies()
        payload = self.get_live_query_template()
        payload["HasKeyWord"] = ""
        payload["BeginDate"] = ""
        payload["EndDate"] = ""
        payload["BeginTime"] = "00:00"
        payload["EndTime"] = "23:59"
        payload["page"] = 1
        payload["pagesize"] = 50
        payload["PlanName"] = ""
        payload["GuestRemark"] = ""
        payload["EtourSEO_Field"] = "OutCode"
        payload["EtourSEO_Value"] = order_no
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": "https://t16.et818.com/XWJ/SysMain",
                "Origin": "https://t16.et818.com",
            }
        )
        resp = session.post(SEARCH_ENDPOINT, data=payload, cookies=cookies, timeout=30)
        if resp.status_code != 200:
            raise Et818BridgeError(f"ET818 搜索接口返回异常：HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception as e:
            raise Et818BridgeError("ET818 搜索接口未返回有效 JSON") from e
        return {"request_query": payload, "response": data}

    def find_by_order_no(self, order_no: str) -> list[Et818OrderMatch]:
        order_no = (order_no or "").strip()
        if not order_no:
            raise Et818BridgeError("订单号不能为空")
        self.ensure_sysmain()
        result = self._search_via_requests(order_no)
        rows = result.get("response", {}).get("Rows") or []
        matches: list[Et818OrderMatch] = []
        for r in rows:
            out_code = str(r.get("OutCode") or r.get("RegCode") or "")
            line_name = str(r.get("PlanName") or r.get("MainlineName") or "")
            guest_summary = str(r.get("GuestCount") or r.get("GuestContact") or "")
            if order_no not in out_code and order_no not in line_name and order_no not in guest_summary:
                continue
            reg_id = int(r.get("RegID") or 0)
            if not reg_id:
                continue
            phone = str(r.get("GuestContact") or r.get("SupplierUserTel") or r.get("SupplierUserMobile") or "")
            if phone == order_no[:11]:
                phone = ""
            matches.append(
                Et818OrderMatch(
                    order_no=out_code or order_no,
                    reg_id=reg_id,
                    biz_mode=int(r["BizMode"]) if r.get("BizMode") not in (None, "") else None,
                    plan_id=int(r["FirstPlanID"]) if r.get("FirstPlanID") not in (None, "") else 0,
                    line_name=line_name,
                    guest_summary=guest_summary,
                    departure_date=str(r.get("BeginDate") or r.get("PlanBeginDate") or ""),
                    phone=phone,
                    raw=r,
                )
            )
        if not matches:
            raise Et818BridgeError("未找到该订单号；本次搜索已绕过页面筛选与前端组件，不需要你手动清日期")
        uniq: dict[tuple[int, int | None, int | None], Et818OrderMatch] = {}
        for r in matches:
            uniq[(r.reg_id, r.biz_mode, r.plan_id)] = r
        return list(uniq.values())

    def build_detail_url(self, reg_id: int, biz_mode: int | None = 15, plan_id: int | None = 0) -> str:
        mode = 15 if biz_mode in (None, "") else int(biz_mode)
        plan = 0 if plan_id in (None, "") else int(plan_id)
        return DETAIL_URL_TEMPLATE.format(plan_id=plan, biz_mode=mode, reg_id=int(reg_id))

    def build_edit_url(self, reg_id: int, biz_mode: int | None = 15, plan_id: int | None = 0) -> str:
        mode = 15 if biz_mode in (None, "") else int(biz_mode)
        plan = 0 if plan_id in (None, "") else int(plan_id)
        return EDIT_URL_TEMPLATE.format(plan_id=plan, biz_mode=mode, reg_id=int(reg_id))

    def open_detail(self, reg_id: int, biz_mode: int | None = 15, plan_id: int | None = 0) -> str:
        detail_url = self.build_detail_url(reg_id=reg_id, biz_mode=biz_mode, plan_id=plan_id)
        escaped = json.dumps(detail_url)
        expr = f'''(() => {{
          const url = {escaped};
          const text = 'ET818详情-' + String({int(reg_id)});
          if (window.layui && layui.index && typeof layui.index.openTabsPage === 'function') {{
            layui.index.openTabsPage(url, text);
            return {{ ok: true, mode: 'layui.index.openTabsPage', detail_url: url }};
          }}
          const body = document.querySelector('#LAY_app_body') || document.body;
          const iframe = document.createElement('iframe');
          iframe.src = url;
          iframe.className = 'layadmin-iframe';
          iframe.style.width = '100%';
          iframe.style.height = '100vh';
          body.appendChild(iframe);
          return {{ ok: true, mode: 'fallback-iframe', detail_url: url }};
        }})()'''
        data = self.eval(expr, timeout=20)
        if not isinstance(data, dict) or not data.get("ok"):
            raise Et818BridgeError("已命中订单，但打开详情页失败")
        return data.get("detail_url") or detail_url

    def open_edit(self, reg_id: int, biz_mode: int | None = 15, plan_id: int | None = 0) -> str:
        edit_url = self.build_edit_url(reg_id=reg_id, biz_mode=biz_mode, plan_id=plan_id)
        escaped = json.dumps(edit_url)
        expr = f'''(() => {{
          const url = {escaped};
          const text = 'ET818编辑-' + String({int(reg_id)});
          if (window.layui && layui.index && typeof layui.index.openTabsPage === 'function') {{
            layui.index.openTabsPage(url, text);
            return {{ ok: true, mode: 'layui.index.openTabsPage', edit_url: url }};
          }}
          const body = document.querySelector('#LAY_app_body') || document.body;
          const iframe = document.createElement('iframe');
          iframe.src = url;
          iframe.className = 'layadmin-iframe';
          iframe.style.width = '100%';
          iframe.style.height = '100vh';
          body.appendChild(iframe);
          return {{ ok: true, mode: 'fallback-iframe', edit_url: url }};
        }})()'''
        data = self.eval(expr, timeout=20)
        if not isinstance(data, dict) or not data.get("ok"):
            raise Et818BridgeError("已命中订单，但打开编辑页失败")
        return data.get("edit_url") or edit_url
