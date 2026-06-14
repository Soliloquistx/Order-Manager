from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from config import STATIC_DIR, TEMPLATES_DIR
from database import init_db
from et818_bridge import Et818BridgeError
from et818_service import (
    build_et818_payload_response,
    et818_autofill_main,
    et818_autofill_prepare,
    et818_autofill_template,
    find_et818_order_by_order_no,
    open_et818_detail_by_order_no,
    open_et818_detail_by_reg_id,
    open_et818_edit_by_reg_id,
    sync_vbk_detail_to_local,
)
from repository import (
    add_note,
    create_order,
    export_orders_csv,
    get_order,
    get_order_by_order_no,
    get_stats,
    import_orders_csv_text,
    list_logs,
    list_notes,
    list_orders,
    list_users,
    patch_status,
    patch_workspace_fields,
    update_order,
    upsert_order,
)
from schemas import (
    Et818AutofillActionPayload,
    Et818OpenDetailPayload,
    Et818OrderLookup,
    NoteCreate,
    OrderCreate,
    OrderUpdate,
    StatusPatch,
    VbkDetailSyncPayload,
    VbkOrderLookupPayload,
    WorkspaceFieldPatch,
)
from vbk_bridge import ChromeVbkBridge, VbkBridgeError, build_order_payload_from_vbk

app = FastAPI(title="Order Manager MVP")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def startup_event():
    init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    html = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/api/users")
def api_users():
    return list_users()


@app.get("/api/orders")
def api_orders(request: Request):
    params = dict(request.query_params)
    return {
        "orders": list_orders(params),
        "stats": get_stats(params),
    }


@app.post("/api/orders")
def api_create_order(payload: OrderCreate):
    try:
        order_id = create_order(payload.model_dump())
        return {"ok": True, "order_id": order_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/orders/export")
def api_export_orders(request: Request):
    params = dict(request.query_params)
    csv_text = export_orders_csv(params)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="orders-export.csv"'},
    )


@app.post("/api/orders/import")
async def api_import_orders(file: UploadFile = File(...)):
    try:
        raw = await file.read()
        text = raw.decode("utf-8-sig")
        result = import_orders_csv_text(text)
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/orders/{order_id}")
def api_get_order(order_id: int):
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return {
        "order": order,
        "notes": list_notes(order_id),
        "logs": list_logs(order_id),
    }


@app.put("/api/orders/{order_id}")
def api_update_order(order_id: int, payload: OrderUpdate):
    try:
        update_order(order_id, payload.model_dump())
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/orders/{order_id}/notes")
def api_add_note(order_id: int, payload: NoteCreate):
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    try:
        from database import db_cursor
        with db_cursor() as conn:
            add_note(
                conn,
                order_id=order_id,
                note_type=payload.note_type,
                content=payload.content,
                follow_status_after=payload.follow_status_after,
                next_follow_up_at=payload.next_follow_up_at,
                created_by=payload.created_by,
            )
            if payload.follow_status_after and payload.follow_status_after != order.get("follow_status"):
                patch_status(order_id, {"follow_status": payload.follow_status_after}, created_by=payload.created_by)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/orders/{order_id}/status")
def api_patch_status(order_id: int, payload: StatusPatch):
    try:
        patch_status(order_id, payload.model_dump())
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.patch("/api/orders/{order_id}/workspace")
def api_patch_workspace(order_id: int, payload: WorkspaceFieldPatch):
    try:
        patch_workspace_fields(order_id, payload.model_dump(exclude_none=True))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/vbk/find-by-order-no")
def api_vbk_find_by_order_no(payload: VbkOrderLookupPayload):
    order_no = payload.order_no.strip()
    tried_list_kinds: list[str] = []
    list_kinds = [payload.list_kind] if payload.list_kind in {"order", "hold"} else ["order", "hold"]
    bridge = ChromeVbkBridge()

    last_error = ""
    for kind in list_kinds:
        tried_list_kinds.append(kind)
        try:
            item = bridge.search_order_by_order_no(order_no, list_kind=kind)
            if not item:
                continue
            local_order = get_order_by_order_no(order_no)
            action = "existing"
            synced = False
            if not local_order and payload.sync_if_missing:
                new_payload = build_order_payload_from_vbk(item)
                order_id, action = upsert_order(new_payload)
                local_order = get_order(order_id)
                synced = True
            return {
                "ok": True,
                "source": "vbk",
                "order": local_order,
                "synced": synced,
                "action": action,
                "matched_list_kind": kind,
                "tried_list_kinds": tried_list_kinds,
                "vbk_item": {
                    "order_no": item.order_no,
                    "product_id": item.product_id,
                    "product_name": item.product_name,
                    "customer_name": item.customer_name,
                    "departure_date": item.departure_date,
                },
            }
        except VbkBridgeError as e:
            last_error = str(e)
    raise HTTPException(status_code=400, detail=last_error or f"未在 VBK 找到订单号 {order_no}")


@app.post("/api/orders/{order_id}/sync-vbk-detail")
def api_sync_vbk_detail(order_id: int, payload: VbkDetailSyncPayload):
    try:
        return sync_vbk_detail_to_local(order_id, payload.model_dump())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/orders/{order_id}/sync-vbk-detail-from-browser")
def api_sync_vbk_detail_from_browser(order_id: int, payload: VbkOrderLookupPayload):
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    order_no = (payload.order_no or order.get("order_no") or "").strip()
    if not order_no:
        raise HTTPException(status_code=400, detail="缺少订单号")

    bridge = ChromeVbkBridge()
    tried_list_kinds: list[str] = []
    list_kinds = [payload.list_kind] if payload.list_kind in {"order", "hold"} else ["order", "hold"]
    last_error = ""

    for kind in list_kinds:
        tried_list_kinds.append(kind)
        try:
            detail_data = bridge.extract_detail_by_order_no(order_no, list_kind=kind)
            sync_result = sync_vbk_detail_to_local(order_id, detail_data)
            return {
                "ok": True,
                "order_id": order_id,
                "order_no": order_no,
                "matched_list_kind": kind,
                "tried_list_kinds": tried_list_kinds,
                "sync_result": sync_result,
            }
        except VbkBridgeError as e:
            last_error = str(e)

    raise HTTPException(status_code=400, detail=last_error or "VBK 详情抓取失败")


@app.get("/api/orders/{order_id}/et818-payload")
def api_get_et818_payload(order_id: int):
    try:
        return build_et818_payload_response(order_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/orders/{order_id}/et818-autofill/prepare")
def api_et818_autofill_prepare(order_id: int):
    try:
        return et818_autofill_prepare(order_id).model_dump()
    except HTTPException:
        raise
    except Et818BridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/orders/{order_id}/et818-autofill/template")
def api_et818_autofill_template(order_id: int, payload: Et818AutofillActionPayload):
    try:
        return et818_autofill_template(order_id, payload).model_dump()
    except HTTPException:
        raise
    except Et818BridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/orders/{order_id}/et818-autofill/main")
def api_et818_autofill_main(order_id: int, payload: Et818AutofillActionPayload):
    try:
        return et818_autofill_main(order_id, payload).model_dump()
    except HTTPException:
        raise
    except Et818BridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/et818/find-by-order-no")
def api_et818_find_by_order_no(payload: Et818OrderLookup):
    try:
        matches = find_et818_order_by_order_no(payload.order_no)
        return {"ok": True, "matches": matches}
    except Et818BridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/et818/open-detail")
def api_et818_open_detail(payload: Et818OpenDetailPayload):
    try:
        return open_et818_detail_by_reg_id(
            reg_id=payload.reg_id,
            biz_mode=payload.biz_mode,
            plan_id=payload.plan_id,
        )
    except Et818BridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/et818/open-edit")
def api_et818_open_edit(payload: Et818OpenDetailPayload):
    try:
        return open_et818_edit_by_reg_id(
            reg_id=payload.reg_id,
            biz_mode=payload.biz_mode,
            plan_id=payload.plan_id,
        )
    except Et818BridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/et818/open-by-order-no")
def api_et818_open_by_order_no(payload: Et818OrderLookup):
    try:
        return open_et818_detail_by_order_no(payload.order_no)
    except Et818BridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/health")
def health():
    return {"ok": True, "service": "order-manager"}
