from typing import Any
import csv
import io
from database import db_cursor, utc_now_str


def row_to_dict(row) -> dict[str, Any]:
    return dict(row) if row else {}


def list_users() -> list[dict[str, Any]]:
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT id, username, display_name, role FROM users WHERE is_active = 1 ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def build_order_filters(params: dict[str, str]) -> tuple[str, list[Any]]:
    clauses = ["o.is_archived = 0"]
    values: list[Any] = []

    keyword = (params.get("keyword") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        clauses.append("(o.order_no LIKE ? OR o.product_name LIKE ? OR o.customer_name LIKE ? OR COALESCE(o.customer_phone, '') LIKE ?)")
        values.extend([like, like, like, like])

    for field in ["channel", "payment_status", "order_status", "follow_status", "owner_id"]:
        value = (params.get(field) or "").strip()
        if value:
            clauses.append(f"o.{field} = ?")
            values.append(value)

    departure_from = (params.get("departure_from") or "").strip()
    departure_to = (params.get("departure_to") or "").strip()
    if departure_from:
        clauses.append("o.departure_date >= ?")
        values.append(departure_from)
    if departure_to:
        clauses.append("o.departure_date <= ?")
        values.append(departure_to)

    return " AND ".join(clauses), values


def list_orders(params: dict[str, str]) -> list[dict[str, Any]]:
    where_sql, values = build_order_filters(params)
    with db_cursor() as conn:
        rows = conn.execute(
            f'''
            SELECT o.*, u.display_name AS owner_name,
                   (o.adult_count + o.child_count) AS traveler_count,
                   COALESCE(o.total_amount, 0) - COALESCE(o.paid_amount, 0) AS unpaid_amount
            FROM orders o
            LEFT JOIN users u ON u.id = o.owner_id
            WHERE {where_sql}
            ORDER BY o.updated_at DESC, o.id DESC
            ''' ,
            values,
        ).fetchall()
        return [dict(r) for r in rows]


def get_order(order_id: int) -> dict[str, Any] | None:
    with db_cursor() as conn:
        row = conn.execute(
            '''
            SELECT o.*, u.display_name AS owner_name,
                   COALESCE(o.total_amount, 0) - COALESCE(o.paid_amount, 0) AS unpaid_amount
            FROM orders o
            LEFT JOIN users u ON u.id = o.owner_id
            WHERE o.id = ?
            ''',
            (order_id,),
        ).fetchone()
        return dict(row) if row else None


def list_notes(order_id: int) -> list[dict[str, Any]]:
    with db_cursor() as conn:
        rows = conn.execute(
            '''
            SELECT n.*, u.display_name AS created_by_name
            FROM order_notes n
            LEFT JOIN users u ON u.id = n.created_by
            WHERE n.order_id = ?
            ORDER BY n.id DESC
            ''',
            (order_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_logs(order_id: int) -> list[dict[str, Any]]:
    with db_cursor() as conn:
        rows = conn.execute(
            '''
            SELECT l.*, u.display_name AS created_by_name
            FROM order_logs l
            LEFT JOIN users u ON u.id = l.created_by
            WHERE l.order_id = ?
            ORDER BY l.id DESC
            ''',
            (order_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_log(conn, order_id: int, action: str, field_name: str = "", old_value: Any = "", new_value: Any = "", description: str = "", created_by: int = 1):
    conn.execute(
        '''
        INSERT INTO order_logs (order_id, action, field_name, old_value, new_value, description, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (order_id, action, field_name, str(old_value or ""), str(new_value or ""), description, created_by, utc_now_str()),
    )


def add_note(conn, order_id: int, note_type: str, content: str, follow_status_after: str = "", next_follow_up_at: str = "", created_by: int = 1):
    now = utc_now_str()
    conn.execute(
        '''
        INSERT INTO order_notes (order_id, note_type, content, follow_status_after, next_follow_up_at, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''',
        (order_id, note_type, content, follow_status_after or None, next_follow_up_at or None, created_by, now),
    )
    conn.execute(
        '''
        UPDATE orders
        SET latest_note_summary = ?, last_follow_up_at = ?, next_follow_up_at = COALESCE(?, next_follow_up_at), updated_at = ?
        WHERE id = ?
        ''',
        (content[:80], now, next_follow_up_at or None, now, order_id),
    )
    create_log(conn, order_id, "add_note", "note", "", content[:80], f"新增备注：{note_type}", created_by)


def create_order(data: dict[str, Any]) -> int:
    now = utc_now_str()
    with db_cursor() as conn:
        cur = conn.execute(
            '''
            INSERT INTO orders (
              order_no, external_order_no, product_name, route_name, channel, source_platform,
              customer_name, customer_phone, backup_contact, customer_note,
              departure_date, return_date, adult_count, child_count, room_count,
              total_amount, paid_amount, currency,
              payment_status, order_status, follow_status, priority,
              owner_id, next_follow_up_at, last_follow_up_at, latest_note_summary,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                data["order_no"], data.get("external_order_no") or None, data["product_name"], data.get("route_name") or None,
                data["channel"], data.get("source_platform") or None,
                data["customer_name"], data.get("customer_phone") or None, data.get("backup_contact") or None, data.get("customer_note") or None,
                data["departure_date"], data.get("return_date") or None, data.get("adult_count") or 1, data.get("child_count") or 0, data.get("room_count"),
                data.get("total_amount"), data.get("paid_amount") or 0, data.get("currency") or "CNY",
                data["payment_status"], data["order_status"], data["follow_status"], data.get("priority") or "普通",
                data.get("owner_id") or 1, data.get("next_follow_up_at") or None, None, None,
                now, now,
            ),
        )
        order_id = cur.lastrowid
        create_log(conn, order_id, "create_order", description="创建订单", created_by=data.get("owner_id") or 1)
        initial_note = (data.get("initial_note") or "").strip()
        if initial_note:
            add_note(conn, order_id, "普通备注", initial_note, created_by=data.get("owner_id") or 1)
        return order_id


def update_order(order_id: int, data: dict[str, Any]) -> None:
    old = get_order(order_id)
    if not old:
        raise ValueError("订单不存在")
    now = utc_now_str()
    with db_cursor() as conn:
        conn.execute(
            '''
            UPDATE orders SET
              order_no=?, external_order_no=?, product_name=?, route_name=?, channel=?, source_platform=?,
              customer_name=?, customer_phone=?, backup_contact=?, customer_note=?,
              departure_date=?, return_date=?, adult_count=?, child_count=?, room_count=?,
              total_amount=?, paid_amount=?, currency=?, payment_status=?, order_status=?, follow_status=?,
              priority=?, owner_id=?, next_follow_up_at=?, updated_at=?
            WHERE id=?
            ''',
            (
                data["order_no"], data.get("external_order_no") or None, data["product_name"], data.get("route_name") or None,
                data["channel"], data.get("source_platform") or None,
                data["customer_name"], data.get("customer_phone") or None, data.get("backup_contact") or None, data.get("customer_note") or None,
                data["departure_date"], data.get("return_date") or None, data.get("adult_count") or 1, data.get("child_count") or 0, data.get("room_count"),
                data.get("total_amount"), data.get("paid_amount") or 0, data.get("currency") or "CNY",
                data["payment_status"], data["order_status"], data["follow_status"], data.get("priority") or "普通",
                data.get("owner_id") or 1, data.get("next_follow_up_at") or None, now, order_id,
            ),
        )
        create_log(conn, order_id, "update_order", description="编辑订单", created_by=data.get("owner_id") or 1)


def patch_status(order_id: int, payload: dict[str, Any], created_by: int = 1) -> None:
    order = get_order(order_id)
    if not order:
        raise ValueError("订单不存在")
    updates = []
    params = []
    fields = ["payment_status", "order_status", "follow_status"]
    with db_cursor() as conn:
        for field in fields:
            new_value = payload.get(field)
            if new_value and new_value != order.get(field):
                updates.append(f"{field} = ?")
                params.append(new_value)
                create_log(conn, order_id, f"update_{field}", field, order.get(field), new_value, f"{field} 更新", created_by)
        if not updates:
            return
        updates.append("updated_at = ?")
        params.append(utc_now_str())
        params.append(order_id)
        conn.execute(f"UPDATE orders SET {', '.join(updates)} WHERE id = ?", params)


def get_stats(params: dict[str, str]) -> dict[str, int]:
    rows = list_orders(params)
    stats = {
        "total": len(rows),
        "待跟进": 0,
        "临近出发": 0,
        "待确认": 0,
        "已支付未确认": 0,
        "超期未跟进": 0,
    }
    for r in rows:
        if r.get("follow_status") == "待跟进":
            stats["待跟进"] += 1
        if r.get("order_status") == "待确认":
            stats["待确认"] += 1
        if r.get("payment_status") == "已支付" and r.get("order_status") != "已确认":
            stats["已支付未确认"] += 1
        if r.get("departure_date"):
            # MVP: 简化规则，按字符串日期近似展示，具体预警后续再加
            pass
    return stats


def export_orders_csv(params: dict[str, str]) -> str:
    rows = list_orders(params)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "订单号", "外部单号", "产品名称", "线路", "客户姓名", "手机号", "渠道", "出发日期", "返程日期",
        "成人数", "儿童数", "房间数", "总金额", "已付金额", "未付金额", "支付状态", "订单状态",
        "跟进状态", "负责人", "最后跟进时间", "最新备注", "创建时间", "更新时间"
    ])
    for r in rows:
        writer.writerow([
            r.get("order_no", ""), r.get("external_order_no", ""), r.get("product_name", ""), r.get("route_name", ""),
            r.get("customer_name", ""), r.get("customer_phone", ""), r.get("channel", ""), r.get("departure_date", ""),
            r.get("return_date", ""), r.get("adult_count", 0), r.get("child_count", 0), r.get("room_count", ""),
            r.get("total_amount", ""), r.get("paid_amount", ""), r.get("unpaid_amount", ""), r.get("payment_status", ""),
            r.get("order_status", ""), r.get("follow_status", ""), r.get("owner_name", ""), r.get("last_follow_up_at", ""),
            r.get("latest_note_summary", ""), r.get("created_at", ""), r.get("updated_at", "")
        ])
    return buffer.getvalue()


def import_orders_csv_text(csv_text: str, default_owner_id: int = 1) -> dict[str, Any]:
    required_fields = ["订单号", "产品名称", "客户姓名", "渠道", "出发日期", "成人数", "支付状态", "订单状态", "跟进状态"]
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头")
    missing = [f for f in required_fields if f not in reader.fieldnames]
    if missing:
        raise ValueError("CSV 缺少必填列: " + ", ".join(missing))

    success = 0
    failed: list[dict[str, Any]] = []
    line_no = 1
    for row in reader:
        line_no += 1
        try:
            payload = {
                "order_no": (row.get("订单号") or "").strip(),
                "external_order_no": (row.get("外部单号") or "").strip(),
                "product_name": (row.get("产品名称") or "").strip(),
                "route_name": (row.get("线路") or "").strip(),
                "channel": (row.get("渠道") or "").strip(),
                "source_platform": (row.get("来源平台") or "").strip(),
                "customer_name": (row.get("客户姓名") or "").strip(),
                "customer_phone": (row.get("手机号") or "").strip(),
                "backup_contact": (row.get("备用联系方式") or "").strip(),
                "customer_note": (row.get("客户备注") or "").strip(),
                "departure_date": (row.get("出发日期") or "").strip(),
                "return_date": (row.get("返程日期") or "").strip(),
                "adult_count": int((row.get("成人数") or "1").strip() or 1),
                "child_count": int((row.get("儿童数") or "0").strip() or 0),
                "room_count": int((row.get("房间数") or "0").strip()) if (row.get("房间数") or "").strip() else None,
                "total_amount": float((row.get("总金额") or "").strip()) if (row.get("总金额") or "").strip() else None,
                "paid_amount": float((row.get("已付金额") or "0").strip() or 0),
                "currency": (row.get("币种") or "CNY").strip() or "CNY",
                "payment_status": (row.get("支付状态") or "").strip(),
                "order_status": (row.get("订单状态") or "").strip(),
                "follow_status": (row.get("跟进状态") or "").strip(),
                "priority": (row.get("优先级") or "普通").strip() or "普通",
                "owner_id": default_owner_id,
                "next_follow_up_at": (row.get("下次跟进时间") or "").strip(),
                "initial_note": (row.get("备注") or "").strip(),
            }
            if not payload["order_no"] or not payload["product_name"] or not payload["customer_name"] or not payload["channel"] or not payload["departure_date"]:
                raise ValueError("存在必填字段为空")
            create_order(payload)
            success += 1
        except Exception as e:
            failed.append({"line": line_no, "error": str(e), "order_no": row.get("订单号", "")})
    return {"success": success, "failed": failed, "total": success + len(failed)}
