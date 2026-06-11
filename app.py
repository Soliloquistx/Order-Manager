from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from config import STATIC_DIR, TEMPLATES_DIR
from database import init_db
from et818_bridge import Et818BridgeError
from et818_service import (
    find_et818_order_by_order_no,
    open_et818_detail_by_order_no,
    open_et818_detail_by_reg_id,
    open_et818_edit_by_reg_id,
)
from repository import (
    add_note,
    create_order,
    export_orders_csv,
    get_order,
    get_stats,
    import_orders_csv_text,
    list_logs,
    list_notes,
    list_orders,
    list_users,
    patch_status,
    update_order,
)
from schemas import (
    Et818OpenDetailPayload,
    Et818OrderLookup,
    NoteCreate,
    OrderCreate,
    OrderUpdate,
    StatusPatch,
)

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


@app.get("/api/health")
def health():
    return {"ok": True, "service": "order-manager"}
