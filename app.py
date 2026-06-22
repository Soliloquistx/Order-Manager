from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
import json

from config import STATIC_DIR, TEMPLATES_DIR
from database import init_db
from et818_bridge import Et818BridgeError
from et818_service import (
    build_et818_payload_response,
    et818_autofill_main,
    et818_autofill_no_save,
    et818_autofill_pickup,
    et818_autofill_prepare,
    et818_autofill_template,
    et818_autofill_travellers,
    et818_autofill_validate,
    et818_submit_after_confirm,
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
    get_vbk_detail_snapshot,
    import_orders_csv_text,
    list_logs,
    list_notes,
    list_orders,
    list_order_pickup_dropoff,
    list_order_travellers,
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


@app.get("/new", response_class=HTMLResponse)
def new_index(request: Request):
    html = (TEMPLATES_DIR / "new-index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/compare/{order_id}", response_class=HTMLResponse)
def compare_page(request: Request, order_id: int):
    html = (TEMPLATES_DIR / "compare.html").read_text(encoding="utf-8")
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


@app.post("/api/vbk/batch-find-by-order-nos")
def api_vbk_batch_find(payload: dict):
    order_nos = [x.strip() for x in (payload.get("order_nos") or []) if x.strip()]
    if not order_nos:
        raise HTTPException(status_code=400, detail="缺少订单号列表")
    list_kind = payload.get("list_kind", "auto")
    sync_if_missing = payload.get("sync_if_missing", True)

    items: list[dict] = []
    total = len(order_nos)
    local_hits = 0
    vbk_hits = 0
    misses = 0
    errors = 0

    for order_no in order_nos:
        try:
            existing = get_order_by_order_no(order_no)
            if existing:
                items.append({"order_no": order_no, "source": "local", "order": existing})
                local_hits += 1
                continue
            # not in local, try VBK
            bridge = ChromeVbkBridge()
            item = None
            for kind in (["order", "hold"] if list_kind == "auto" else [list_kind]):
                item = bridge.search_order_by_order_no(order_no, list_kind=kind)
                if item:
                    break
            if not item:
                items.append({"order_no": order_no, "source": "miss", "detail": "VBK 未找到"})
                misses += 1
                continue
            if sync_if_missing:
                new_payload = build_order_payload_from_vbk(item)
                order_id, _ = upsert_order(new_payload)
                local_order = get_order(order_id)
                items.append({"order_no": order_no, "source": "vbk", "order": local_order, "detail": f"已入库 id={order_id}"})
                vbk_hits += 1
            else:
                items.append({"order_no": order_no, "source": "vbk", "detail": f"已查到（未入库）"})
                vbk_hits += 1
        except Exception as e:
            items.append({"order_no": order_no, "source": "error", "detail": str(e)[:200]})
            errors += 1

    return {
        "ok": True,
        "total": total,
        "local_hits": local_hits,
        "vbk_hits": vbk_hits,
        "misses": misses,
        "errors": errors,
        "items": items,
    }


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


@app.get("/api/orders/{order_id}/compare")
def api_compare(order_id: int):
    """双栏对比：左边 VBK 原始数据，右边 ET818 填表数据"""
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    order_no = order.get("order_no") or ""

    # VBK 原始数据
    vbk_raw_key_fields = {}
    vbk_raw_travellers = []
    vbk_raw_pickup = []

    snapshot = get_vbk_detail_snapshot(order_no)
    if snapshot:
        # 结构化字段
        vbk_raw_key_fields = {
            k: snapshot.get(k)
            for k in (
                "order_type_text", "confirm_status_text", "payment_status_text",
                "departure_date", "return_date", "departure_city",
                "customer_name", "customer_phone",
                "distribution_channel", "scenic_booking_no",
                "reservation_scenic_name", "merchant_note",
            )
        }
        # 原始 JSON 全量展示
        raw_json_str = snapshot.get("raw_json") or ""
        if raw_json_str:
            try:
                parsed = json.loads(raw_json_str)
                vbk_raw_key_fields["_raw_json_parsed"] = parsed
                # 从 raw_json 提取客人和接送（如果 snapshot 的 travellers 表没有的话）
                if "travellers" in parsed:
                    vbk_raw_travellers = parsed["travellers"]
                if "flights" in parsed:
                    vbk_raw_pickup = parsed["flights"]
                if "pickup_dropoff" in parsed:
                    vbk_raw_pickup = parsed["pickup_dropoff"]
            except (json.JSONDecodeError, TypeError):
                pass

    # 补齐 traveller / pickup 从分离表
    if not vbk_raw_travellers:
        vbk_raw_travellers = list_order_travellers(order_id)
    if not vbk_raw_pickup:
        vbk_raw_pickup = list_order_pickup_dropoff(order_id)

    # ET818 填表数据
    try:
        payload_resp = build_et818_payload_response(order_id)
        payload = payload_resp.model_dump() if hasattr(payload_resp, "model_dump") else vars(payload_resp)
    except Exception as e:
        payload = {"ok": False, "detail": str(e)[:200]}

    return {
        "order": {k: order.get(k) for k in (
            "id", "order_no", "product_name", "route_name", "channel",
            "source_platform", "customer_name", "customer_phone",
            "departure_date", "return_date", "adult_count", "child_count",
            "total_amount", "paid_amount", "payment_status", "order_status",
        )},
        "vbk_raw": {
            "key_fields": vbk_raw_key_fields,
            "travellers": vbk_raw_travellers,
            "pickup_dropoff": vbk_raw_pickup,
        },
        "et818": payload,
    }


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


@app.post("/api/orders/{order_id}/et818-autofill/travellers")
def api_et818_autofill_travellers(order_id: int, payload: Et818AutofillActionPayload):
    try:
        return et818_autofill_travellers(order_id, payload).model_dump()
    except HTTPException:
        raise
    except Et818BridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/orders/{order_id}/et818-autofill/pickup")
def api_et818_autofill_pickup(order_id: int, payload: Et818AutofillActionPayload):
    try:
        return et818_autofill_pickup(order_id, payload).model_dump()
    except HTTPException:
        raise
    except Et818BridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/orders/{order_id}/et818-autofill/validate")
def api_et818_autofill_validate(order_id: int, payload: Et818AutofillActionPayload):
    try:
        return et818_autofill_validate(order_id, payload).model_dump()
    except HTTPException:
        raise
    except Et818BridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/orders/{order_id}/et818-autofill/no-save")
def api_et818_autofill_no_save(order_id: int):
    try:
        return et818_autofill_no_save(order_id)
    except HTTPException:
        raise
    except Et818BridgeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/orders/{order_id}/et818-autofill/submit")
def api_et818_submit_after_confirm(order_id: int):
    try:
        return et818_submit_after_confirm(order_id)
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
