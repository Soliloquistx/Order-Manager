from __future__ import annotations

from et818_bridge import ChromeEt818Bridge, Et818BridgeError


bridge = ChromeEt818Bridge()


def find_et818_order_by_order_no(order_no: str) -> list[dict]:
    matches = bridge.find_by_order_no(order_no)
    return [
        {
            "order_no": item.order_no,
            "reg_id": item.reg_id,
            "biz_mode": item.biz_mode,
            "plan_id": item.plan_id,
            "line_name": item.line_name,
            "guest_summary": item.guest_summary,
            "departure_date": item.departure_date,
            "phone": item.phone,
            "detail_url": bridge.build_detail_url(item.reg_id, item.biz_mode, item.plan_id),
        }
        for item in matches
    ]


def open_et818_detail_by_reg_id(reg_id: int, biz_mode: int | None = None, plan_id: int | None = 0) -> dict:
    detail_url = bridge.open_detail(reg_id=reg_id, biz_mode=biz_mode, plan_id=plan_id)
    return {"ok": True, "detail_url": detail_url}


def open_et818_edit_by_reg_id(reg_id: int, biz_mode: int | None = None, plan_id: int | None = 0) -> dict:
    edit_url = bridge.open_edit(reg_id=reg_id, biz_mode=biz_mode, plan_id=plan_id)
    return {"ok": True, "edit_url": edit_url}


def open_et818_detail_by_order_no(order_no: str) -> dict:
    matches = find_et818_order_by_order_no(order_no)
    if not matches:
        raise Et818BridgeError("未找到该订单号")
    if len(matches) > 1:
        return {"ok": False, "need_pick": True, "matches": matches}
    match = matches[0]
    detail_url = bridge.open_detail(
        reg_id=match["reg_id"],
        biz_mode=match.get("biz_mode"),
        plan_id=match.get("plan_id") or 0,
    )
    return {"ok": True, "match": match, "detail_url": detail_url}
