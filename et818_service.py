from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from et818_bridge import ChromeEt818Bridge, Et818BridgeError
from repository import (
    get_order,
    get_vbk_detail_snapshot,
    list_order_pickup_dropoff,
    list_order_travellers,
    replace_order_pickup_dropoff,
    replace_order_travellers,
    upsert_vbk_detail_snapshot,
)
from schemas import (
    DateParts,
    Et818AutofillActionPayload,
    Et818AutofillReport,
    Et818AutofillTarget,
    Et818Payload,
    Et818PayloadResponse,
    MetaBlock,
    MetaDebug,
    NotesBlock,
    OrderInfo,
    PickupDropoffItem,
    RoomNeed,
    TemplateAutofillExpected,
    TemplateMatchBasis,
    TemplateSelection,
    Traveller,
    TravellerSource,
)


ET818_CONTROL_MAP = {
    "线路模板": "输入后点选",
    "大交通": "下拉选择",
    "团队类别": "下拉选择",
    "订单电话/姓名": "直接输入",
    "订单号": "直接输入",
    "出团日期": "直接输入",
    "返程日期": "直接输入",
    "参团人数": "直接输入",
    "用房": "直接输入",
    "客人名单": "表格输入",
    "接送信息": "表格输入",
}


bridge = ChromeEt818Bridge()


def et818_control_mode(field_name: str) -> str:
    return ET818_CONTROL_MAP.get(field_name, "未知")


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


def check_and_update_et818_status(order_id: int) -> dict[str, Any]:
    """Check a single order's 易途 status and update DB."""
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    order_no = (order.get("order_no") or "").strip()
    if not order_no:
        raise HTTPException(status_code=400, detail="订单缺少订单号")

    found = False
    reg_id = None
    try:
        matches = bridge.find_by_order_no(order_no)
        found = len(matches) > 0
        if found:
            reg_id = matches[0].reg_id
    except Et818BridgeError:
        # Bridge error (未登录/未打开等) → 透传出去，不让前端吞掉
        raise
    except Exception:
        # 搜索请求本身异常（网络等）
        raise HTTPException(status_code=500, detail="易途搜索请求异常")

    new_status = "已提交" if found else "未处理"

    # Update the DB
    from database import db_cursor, utc_now_str
    with db_cursor() as conn:
        conn.execute(
            "UPDATE orders SET et818_status = ?, updated_at = ? WHERE id = ?",
            (new_status, utc_now_str(), order_id),
        )

    return {
        "ok": True,
        "order_id": order_id,
        "order_no": order_no,
        "found": found,
        "et818_status": new_status,
        "reg_id": reg_id,
    }


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


def prepare_et818_add_page(order_id: int) -> Et818AutofillTarget:
    order = get_order_or_raise(order_id)
    token = f"of{int(datetime.now().timestamp() * 1000)}"
    add_url = f"/XWJ/PlanReg/AddDSN/PlanMng.ListRegDSN1?time={token}&PageType=add&RegID=0&BizMode=15&FixedPlanClass="
    escaped_url = json.dumps(add_url, ensure_ascii=False)
    escaped_title = json.dumps(f"ET818自动填表-{order.get('order_no', '')}", ensure_ascii=False)
    result = bridge.eval(
        f'''(() => {{
          try {{
            const url = {escaped_url};
            const title = {escaped_title};
            if (window.layui && layui.index && typeof layui.index.openTabsPage === 'function') {{
              layui.index.openTabsPage(url, title);
            }} else {{
              const body = document.querySelector('#LAY_app_body') || document.body;
              const iframe = document.createElement('iframe');
              iframe.src = url;
              iframe.className = 'layadmin-iframe';
              iframe.style.width = '100%';
              iframe.style.height = '100vh';
              body.appendChild(iframe);
            }}
            return {{ ok: true, url }};
          }} catch (e) {{
            return {{ ok: false, detail: e.message || String(e) }};
          }}
        }})()''',
        timeout=20,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise Et818BridgeError((result or {}).get("detail") or "打开 ET818 新增页失败")
    return Et818AutofillTarget(token=token, add_url=add_url)


def et818_autofill_prepare(order_id: int) -> Et818AutofillReport:
    order = get_order_or_raise(order_id)
    target = prepare_et818_add_page(order_id)
    result = bridge.eval(
        f'''(() => new Promise(resolve => setTimeout(() => {{
          try {{
            const token = {json.dumps(target.token, ensure_ascii=False)};
            const frame = [...document.querySelectorAll('iframe')].find(f => (f.src || '').includes(`time=${{token}}`));
            if (!frame || !frame.contentWindow) return resolve({{ ok:false, detail:'未找到新增页 iframe' }});
            const doc = frame.contentWindow.document;
            const bodyText = doc.body?.innerText || '';
            const tables = [...doc.querySelectorAll('table')];
            const mainTable = tables.find(t => (t.innerText || '').includes('订单电话/姓名') && (t.innerText || '').includes('订单号'));
            const readyByTable = !!mainTable;
            const readyByText = bodyText.includes('订单电话/姓名') && bodyText.includes('订单号');
            const pageReady = readyByTable || readyByText;
            resolve({{
              ok:true,
              page_ready: pageReady,
              detail: pageReady ? '新增页已就绪' : '主表仍未出现',
              table_count: tables.length,
              ready_by_table: readyByTable,
              ready_by_text: readyByText,
              body_preview: bodyText.slice(0, 2000)
            }});
          }} catch (e) {{
            resolve({{ ok:false, detail:e.message || String(e) }});
          }}
        }}, 5000)))()''',
        timeout=50,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise Et818BridgeError((result or {}).get("detail") or "ET818 新增页未就绪")
    return Et818AutofillReport(
        ok=True,
        phase="prepare",
        order_id=order_id,
        order_no=order.get("order_no", "") or "",
        token=target.token,
        add_url=target.add_url,
        page_ready=bool(result.get("page_ready")),
        detail=result.get("detail", ""),
        errors=[] if result.get("page_ready") else ['prepare: 主表仍未出现'],
    )


def et818_autofill_template(order_id: int, payload: Et818AutofillActionPayload) -> Et818AutofillReport:
    """
    线路模板固定归类为：输入关键词 -> 下拉候选点选。
    这里不把“输入框里出现文字”当成功，必须点中候选并回读确认已提交。
    """
    order = get_order_or_raise(order_id)
    payload_response = build_et818_payload_response(order_id)
    template_selection = payload_response.et818_payload.template_selection
    keyword = template_selection.template_keyword or template_selection.template_name
    final_name = template_selection.template_name or keyword
    control_mode = et818_control_mode("线路模板")
    result = bridge.eval(
        f'''(() => new Promise(resolve => setTimeout(() => {{
          try {{
            const token = {json.dumps(payload.token, ensure_ascii=False)};
            const keyword = {json.dumps(keyword, ensure_ascii=False)};
            const finalName = {json.dumps(final_name, ensure_ascii=False)};
            const frame = [...document.querySelectorAll('iframe')].find(f => (f.src || '').includes(`time=${{token}}`));
            if (!frame || !frame.contentWindow) return resolve({{ ok:false, detail:'未找到模板页 iframe' }});
            const win = frame.contentWindow;
            const doc = win.document;
            const mainTable = [...doc.querySelectorAll('table')].find(t => (t.innerText || '').includes('线路模板') && (t.innerText || '').includes('订单号'));
            if (!mainTable) return resolve({{ ok:false, detail:'未找到主表' }});
            let valueCell = null;
            for (const row of [...mainTable.rows]) {{
              for (let i = 0; i < row.cells.length; i += 1) {{
                if ((row.cells[i].innerText || '').trim() === '线路模板:') {{
                  valueCell = row.cells[i + 1] || null;
                }}
              }}
            }}
            const input = valueCell ? valueCell.querySelector('input') : null;
            if (!input) return resolve({{ ok:false, detail:'未找到线路模板输入框' }});

            const getItems = () => [...doc.querySelectorAll('.dropdown-item'), ...document.querySelectorAll('.dropdown-item')]
              .filter(el => !!(el.innerText || el.textContent || '').trim());
            const textOf = (el) => (el.innerText || el.textContent || '').trim();
            const setInput = (value) => {{
              input.focus();
              input.value = value == null ? '' : String(value);
              input.dispatchEvent(new win.Event('input', {{ bubbles: true }}));
              input.dispatchEvent(new win.Event('change', {{ bubbles: true }}));
              input.dispatchEvent(new win.Event('keyup', {{ bubbles: true }}));
            }};
            const clickItem = (item) => {{
              if (!item) return false;
              item.scrollIntoView?.({{ block: 'nearest' }});
              item.dispatchEvent(new MouseEvent('mouseenter', {{ bubbles: true }}));
              item.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true }}));
              item.click();
              item.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true }}));
              return true;
            }};
            const findItem = (items) =>
              items.find(el => textOf(el) === finalName)
              || items.find(el => textOf(el).includes(finalName))
              || items.find(el => textOf(el).includes(keyword));

            setInput('');
            setInput(keyword);
            input.dispatchEvent(new win.KeyboardEvent('keydown', {{ key: 'ArrowDown', bubbles: true }}));
            input.dispatchEvent(new win.KeyboardEvent('keyup', {{ key: 'ArrowDown', bubbles: true }}));

            setTimeout(() => {{
              let items = getItems();
              let item = findItem(items);
              if (!item) {{
                input.dispatchEvent(new win.KeyboardEvent('keydown', {{ key: 'ArrowDown', bubbles: true }}));
                input.dispatchEvent(new win.KeyboardEvent('keyup', {{ key: 'ArrowDown', bubbles: true }}));
                items = getItems();
                item = findItem(items);
              }}
              if (!item) return resolve({{ ok:false, detail:'未出现匹配的线路模板候选', candidates: items.map(textOf).slice(0,20), keyword, finalName }});
              const clicked = clickItem(item);
              setTimeout(() => {{
                const committed = input.value || '';
                const matched = committed.includes(finalName) || committed.includes(keyword);
                if (!clicked || !matched) {{
                  return resolve({{ ok:false, detail:'线路模板候选未实际选中', candidates: getItems().map(textOf).slice(0,20), committed_value: committed, keyword, finalName }});
                }}
                resolve({{ ok:true, detail:'线路模板已选中', template_name: finalName, committed_value: committed }});
              }}, 800);
            }}, 1200);
          }} catch (e) {{
            resolve({{ ok:false, detail:e.message || String(e) }});
          }}
        }}, 200)))()''',
        timeout=40,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise Et818BridgeError((result or {}).get("detail") or "线路模板选择失败")
    return Et818AutofillReport(
        ok=True,
        phase="template",
        order_id=order_id,
        order_no=order.get("order_no", "") or "",
        token=payload.token,
        add_url=payload.add_url,
        template_name=result.get("template_name", final_name),
        errors=[] if control_mode == "输入后点选" else [f"线路模板控件类型异常: {control_mode}"],
        detail=result.get("detail", ""),
    )


def et818_autofill_main(order_id: int, payload: Et818AutofillActionPayload) -> Et818AutofillReport:
    """
    ET818 主信息区只按控件类型处理，不再临场猜控件：
    - 直接输入：订单电话/姓名、订单号、日期、人数、用房
    - 下拉选择：大交通、团队类别（必须真实点选候选项）
    - 输入后点选：线路模板（在 template 阶段处理）
    """
    order = get_order_or_raise(order_id)
    payload_response = build_et818_payload_response(order_id)
    et = payload_response.et818_payload
    order_info = et.order_info
    transport_mode = et818_control_mode("大交通")
    team_mode = et818_control_mode("团队类别")

    result = bridge.eval(
        f'''(() => new Promise(resolve => setTimeout(() => {{
          try {{
            const token = {json.dumps(payload.token, ensure_ascii=False)};
            const data = {json.dumps({
                'contact_name': order_info.contact_name,
                'departure_date': order_info.departure_date.model_dump(),
                'return_date': order_info.return_date.model_dump(),
                'transport_name': order_info.transport_name,
                'order_no': order_info.order_no,
                'team_category': order_info.team_category,
                'adult_count': order_info.adult_count,
                'child_count': order_info.child_count,
                'room_need': order_info.room_need.model_dump(),
            }, ensure_ascii=False)};
            const frame = [...document.querySelectorAll('iframe')].find(f => (f.src || '').includes(`time=${{token}}`));
            if (!frame || !frame.contentWindow) return resolve({{ ok:false, detail:'未找到主信息页 iframe' }});
            const win = frame.contentWindow;
            const doc = win.document;
            const mainTable = [...doc.querySelectorAll('table')].find(t => (t.innerText || '').includes('订单电话/姓名') && (t.innerText || '').includes('团队类别'));
            if (!mainTable) return resolve({{ ok:false, detail:'未找到订单信息主表' }});

            const setValue = (el, value) => {{
              if (!el) return;
              const v = value == null ? '' : String(value);
              el.value = v;
              el.dispatchEvent(new win.Event('input', {{ bubbles: true }}));
              el.dispatchEvent(new win.Event('change', {{ bubbles: true }}));
              el.dispatchEvent(new win.Event('blur', {{ bubbles: true }}));
            }};
            const getValueCellByLabel = (label) => {{
              for (const row of [...mainTable.rows]) {{
                for (let i = 0; i < row.cells.length; i += 1) {{
                  if ((row.cells[i].innerText || '').trim() === label) return row.cells[i + 1] || null;
                }}
              }}
              return null;
            }};
            const setDateCell = (cell, parts) => {{
              if (!cell || !parts) return;
              const inputs = [...cell.querySelectorAll('input')];
              if (inputs[0]) setValue(inputs[0], parts.year || '');
              if (inputs[1]) setValue(inputs[1], parts.month || '');
              if (inputs[2]) setValue(inputs[2], parts.day || '');
            }};
            const pickDropdown = (cell, finalText) => {{
              if (!cell || !finalText) return Promise.resolve({{ ok:false, committed:'', matched:false, candidates:[] }});
              const input = cell.querySelector('input');
              if (!input) return Promise.resolve({{ ok:false, committed:'', matched:false, candidates:[] }});
              const getItems = () => [...doc.querySelectorAll('.dropdown-item'), ...document.querySelectorAll('.dropdown-item')]
                .filter(el => !!(el.innerText || el.textContent || '').trim());
              const textOf = (el) => (el.innerText || el.textContent || '').trim();
              const findItem = (items) =>
                items.find(el => textOf(el) === finalText)
                || items.find(el => textOf(el).includes(finalText));
              const clickItem = (item) => {{
                if (!item) return false;
                item.scrollIntoView?.({{ block: 'nearest' }});
                item.dispatchEvent(new MouseEvent('mouseenter', {{ bubbles: true }}));
                item.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true }}));
                item.click();
                item.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true }}));
                return true;
              }};
              input.focus();
              setValue(input, finalText);
              input.dispatchEvent(new win.KeyboardEvent('keydown', {{ key: 'ArrowDown', bubbles: true }}));
              input.dispatchEvent(new win.KeyboardEvent('keyup', {{ key: 'ArrowDown', bubbles: true }}));
              return new Promise(resolvePick => setTimeout(() => {{
                let items = getItems();
                let item = findItem(items);
                if (!item) {{
                  input.dispatchEvent(new win.KeyboardEvent('keydown', {{ key: 'ArrowDown', bubbles: true }}));
                  input.dispatchEvent(new win.KeyboardEvent('keyup', {{ key: 'ArrowDown', bubbles: true }}));
                  items = getItems();
                  item = findItem(items);
                }}
                const clicked = clickItem(item);
                setTimeout(() => {{
                  const committed = input.value || '';
                  const matched = committed.includes(finalText);
                  resolvePick({{ ok: clicked && matched, committed, matched, candidates: getItems().map(textOf).slice(0,20) }});
                }}, 600);
              }}, 900));
            }};

            setValue(getValueCellByLabel('订单电话/姓名:')?.querySelector('input'), data.contact_name);
            setDateCell(getValueCellByLabel('出团日期:'), data.departure_date);
            setDateCell(getValueCellByLabel('返程日期:'), data.return_date);
            setValue(getValueCellByLabel('订单号:')?.querySelector('input'), data.order_no);

            Promise.all([
              pickDropdown(getValueCellByLabel('大交通: *'), data.transport_name),
              pickDropdown(getValueCellByLabel('团队类别:'), data.team_category),
            ]).then(([transportResult, teamResult]) => {{
              const transportOk = !!transportResult.ok;
              const teamOk = !!teamResult.ok;

              const peopleCell = getValueCellByLabel('参团人数: *');
              const peopleInputs = peopleCell ? [...peopleCell.querySelectorAll('input')] : [];
              if (peopleInputs[0]) setValue(peopleInputs[0], data.adult_count);
              if (peopleInputs[1]) setValue(peopleInputs[1], data.child_count);

              const roomCell = getValueCellByLabel('用房: *');
              const roomSelect = roomCell?.querySelector('select');
              if (roomSelect) {{ roomSelect.value = '三星'; roomSelect.dispatchEvent(new Event('change', {{ bubbles: true }})); }}
              const roomInputs = roomCell ? [...roomCell.querySelectorAll('input')] : [];
              if (roomInputs[0]) setValue(roomInputs[0], data.room_need.standard);
              if (roomInputs[1]) setValue(roomInputs[1], data.room_need.big_bed);
              if (roomInputs[2]) setValue(roomInputs[2], data.room_need.triple);
              if (roomInputs[3]) setValue(roomInputs[3], data.room_need.single_female);
              if (roomInputs[4]) setValue(roomInputs[4], data.room_need.single_male);
              if (roomInputs[5]) setValue(roomInputs[5], data.room_need.nights);

              const readDate = (cell) => {{
                const inputs = cell ? [...cell.querySelectorAll('input')] : [];
                return [inputs[0]?.value || '', inputs[1]?.value || '', inputs[2]?.value || ''];
              }};
              const requiredMain = [
                {{ label:'订单电话/姓名', value:getValueCellByLabel('订单电话/姓名:')?.querySelector('input')?.value || '' }},
                {{ label:'出团日期', value:readDate(getValueCellByLabel('出团日期:')).join('-') }},
                {{ label:'返程日期', value:readDate(getValueCellByLabel('返程日期:')).join('-') }},
                {{ label:'大交通', value:getValueCellByLabel('大交通: *')?.querySelector('input')?.value || '' }},
                {{ label:'订单号', value:getValueCellByLabel('订单号:')?.querySelector('input')?.value || '' }},
                {{ label:'团队类别', value:getValueCellByLabel('团队类别:')?.querySelector('input')?.value || '' }},
                {{ label:'参团人数', value:peopleInputs.map(x => x.value || '').join('/') }},
                {{ label:'用房', value:roomInputs.map(x => x.value || '').join('/') }},
              ];

              resolve({{
                ok:true,
                detail:'主信息已填写',
                transport_name:data.transport_name,
                team_category:data.team_category,
                required_main: requiredMain,
                transport_selected: transportOk,
                team_selected: teamOk,
                transport_committed: transportResult.committed || '',
                team_committed: teamResult.committed || '',
                dropdown_errors: [
                  ...(!transportOk ? [{{ field:'大交通', candidates: transportResult.candidates || [], committed: transportResult.committed || '' }}] : []),
                  ...(!teamOk ? [{{ field:'团队类别', candidates: teamResult.candidates || [], committed: teamResult.committed || '' }}] : []),
                ],
              }});
            }});
          }} catch (e) {{
            resolve({{ ok:false, detail:e.message || String(e) }});
          }}
        }}, 400)))()''',
        timeout=50,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise Et818BridgeError((result or {}).get("detail") or "主信息填写失败")
    return Et818AutofillReport(
        ok=True,
        phase="main",
        order_id=order_id,
        order_no=order.get("order_no", "") or "",
        token=payload.token,
        add_url=payload.add_url,
        transport_name=result.get("transport_name", order_info.transport_name),
        team_category=result.get("team_category", order_info.team_category),
        required_main=result.get("required_main", []),
        errors=[
            *([f"大交通未实际选中: {result.get('transport_committed','')}" ] if not result.get("transport_selected", False) else []),
            *([f"团队类别未实际选中: {result.get('team_committed','')}" ] if not result.get("team_selected", False) else []),
            *([f"大交通控件类型异常: {transport_mode}"] if transport_mode != "下拉选择" else []),
            *([f"团队类别控件类型异常: {team_mode}"] if team_mode != "下拉选择" else []),
        ],
        detail=result.get("detail", ""),
    )


def et818_autofill_travellers(order_id: int, payload: Et818AutofillActionPayload) -> Et818AutofillReport:
    order = get_order_or_raise(order_id)
    payload_response = build_et818_payload_response(order_id)
    travellers = payload_response.et818_payload.travellers
    traveller_count = len(travellers)

    if not travellers:
        raise Et818BridgeError("没有客人数据，无法填写客人名单")

    travellers_json = json.dumps([{
        'name': t.name,
        'phone': '',
        'id_type': t.id_type,
        'id_no': t.id_no,
        'gender': t.gender,
        'birth_date': t.birth_date.model_dump(),
        'age': t.age,
        'native_place': t.native_place,
        'note': t.note,
        'person_type': t.person_type,
    } for t in travellers], ensure_ascii=False)

    result = bridge.eval(
        f'''(() => new Promise(resolve => setTimeout(() => {{
          try {{
            const token = {json.dumps(payload.token, ensure_ascii=False)};
            const travellers = {travellers_json};
            const frame = [...document.querySelectorAll('iframe')].find(f => (f.src || '').includes(`time=${{token}}`));
            if (!frame || !frame.contentWindow) return resolve({{ ok:false, detail:'未找到客人页 iframe' }});
            const win = frame.contentWindow;
            const doc = win.document;

            const guestTables = [...doc.querySelectorAll('#guestTable')];
            const bodyTable = guestTables.find(t => t.querySelector('tbody'));
            if (!bodyTable) return resolve({{ ok:false, detail:'未找到客人 body table' }});

            const tbody = bodyTable.querySelector('tbody');
            const rows = [...tbody.querySelectorAll('tr')];

            const fillInput = (cell, idx, value) => {{
              const el = cell?.querySelectorAll('input')[idx];
              if (el) {{ el.value = String(value||''); el.dispatchEvent(new win.Event('input',{{bubbles:true}})); el.dispatchEvent(new win.Event('change',{{bubbles:true}})); }}
            }};
            const fillSelect = (cell, value) => {{
              const sel = cell?.querySelector('select');
              if (sel && value) {{ sel.value = value; sel.dispatchEvent(new win.Event('change',{{bubbles:true}})); }}
            }};
            const fillDate = (cell, parts) => {{
              if (!cell || !parts) return;
              const inputs = [...cell.querySelectorAll('input')];
              if (inputs[0]) {{ inputs[0].value = parts.year||''; inputs[0].dispatchEvent(new win.Event('input',{{bubbles:true}})); }}
              if (inputs[1]) {{ inputs[1].value = parts.month||''; inputs[1].dispatchEvent(new win.Event('input',{{bubbles:true}})); }}
              if (inputs[2]) {{ inputs[2].value = parts.day||''; inputs[2].dispatchEvent(new win.Event('input',{{bubbles:true}})); }}
            }};

            const results = [];
            for (let ri = 0; ri < travellers.length && ri < rows.length; ri += 1) {{
              const cells = [...rows[ri].querySelectorAll('td')];
              const t = travellers[ri];
              const cell = (n) => cells[n] || null;
              fillInput(cell(1), 0, t.name);
              fillSelect(cell(3), t.id_type);
              fillInput(cell(4), 0, t.id_no);
              fillSelect(cell(5), t.gender);
              fillDate(cell(6), t.birth_date);
              fillInput(cell(7), 0, t.age);
              fillInput(cell(9), 0, t.native_place);
              fillInput(cell(10), 0, t.note);
              const rn = cell(1)?.querySelector('input')?.value || '';
              const rid = cell(4)?.querySelector('input')?.value || '';
              results.push({{ name: rn, id_no: rid }});
            }}

            resolve({{
              ok:true,
              detail:'客人名单已填写',
              traveller_count: travellers.length,
              filled: results,
            }});
          }} catch (e) {{
            resolve({{ ok:false, detail:e.message || String(e) }});
          }}
        }}, 500)))()''',
        timeout=50,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise Et818BridgeError((result or {}).get("detail") or "客人名单填写失败")
    return Et818AutofillReport(
        ok=True,
        phase="travellers",
        order_id=order_id,
        order_no=order.get("order_no", "") or "",
        token=payload.token,
        add_url=payload.add_url,
        traveller_count=result.get("traveller_count", traveller_count),
        detail=result.get("detail", ""),
    )


def et818_autofill_pickup(order_id: int, payload: Et818AutofillActionPayload) -> Et818AutofillReport:
    order = get_order_or_raise(order_id)
    payload_response = build_et818_payload_response(order_id)
    pickup_items = payload_response.et818_payload.pickup_dropoff
    pickup_count = len(pickup_items)

    if not pickup_items:
        raise Et818BridgeError("没有接送数据")

    pickups_json = json.dumps([{
        'action': p.action,
        'date': p.date.model_dump(),
        'location': p.location,
        'flight_no': p.flight_no,
        'time': p.time,
        'description': p.description,
    } for p in pickup_items], ensure_ascii=False)

    result = bridge.eval(
        f'''(() => new Promise(resolve => setTimeout(() => {{
          try {{
            const token = {json.dumps(payload.token, ensure_ascii=False)};
            const pickups = {pickups_json};
            const rowIndexes = [1, 4];
            const frame = [...document.querySelectorAll('iframe')].find(f => (f.src || '').includes(`time=${{token}}`));
            if (!frame || !frame.contentWindow) return resolve({{ ok:false, detail:'未找到接送页 iframe' }});
            const win = frame.contentWindow;
            const doc = win.document;

            const pickupTable = [...doc.querySelectorAll('table')].find(t => (t.innerText || '').includes('班次时间') && (t.innerText || '').includes('接送描述'));
            if (!pickupTable) return resolve({{ ok:false, detail:'未找到订单接送表' }});

            const rows = [...pickupTable.querySelectorAll('tr')];
            const fillInput = (cell, value) => {{
              const el = cell?.querySelector('input');
              if (el) {{ el.value = String(value||''); el.dispatchEvent(new win.Event('input',{{bubbles:true}})); el.dispatchEvent(new win.Event('change',{{bubbles:true}})); }}
            }};
            const fillSelect = (cell, value) => {{
              const sel = cell?.querySelector('select');
              if (sel && value) {{ sel.value = value; sel.dispatchEvent(new win.Event('change',{{bubbles:true}})); }}
            }};
            const fillDate = (cell, parts) => {{
              if (!cell || !parts) return;
              const inputs = [...cell.querySelectorAll('input')];
              if (inputs[0]) {{ inputs[0].value = parts.year||''; inputs[0].dispatchEvent(new win.Event('input',{{bubbles:true}})); }}
              if (inputs[1]) {{ inputs[1].value = parts.month||''; inputs[1].dispatchEvent(new win.Event('input',{{bubbles:true}})); }}
              if (inputs[2]) {{ inputs[2].value = parts.day||''; inputs[2].dispatchEvent(new win.Event('input',{{bubbles:true}})); }}
            }};

            const results = [];
            for (let pi = 0; pi < pickups.length && pi < rowIndexes.length; pi += 1) {{
              const row = rows[rowIndexes[pi]];
              if (!row) continue;
              const cells = [...row.querySelectorAll('td')];
              const p = pickups[pi];
              const cell = (n) => cells[n] || null;
              const actEl = cell(1)?.querySelector('select') || cell(1)?.querySelector('input');
              if (actEl?.tagName === 'SELECT') fillSelect(cell(1), p.action);
              else fillInput(cell(1), p.action);
              fillDate(cell(2), p.date);
              fillInput(cell(3), p.location);
              fillInput(cell(4), p.flight_no);
              fillInput(cell(5), p.time);
              fillInput(cell(6), p.description || '');

              const ra = cell(1)?.querySelector('select, input')?.value || '';
              const rf = cell(4)?.querySelector('input')?.value || '';
              results.push({{ action: ra, flight: rf }});
            }}

            resolve({{
              ok:true,
              detail:'接送信息已填写',
              pickup_count: pickups.length,
              filled: results,
            }});
          }} catch (e) {{
            resolve({{ ok:false, detail:e.message || String(e) }});
          }}
        }}, 500)))()''',
        timeout=50,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise Et818BridgeError((result or {}).get("detail") or "接送信息填写失败")
    return Et818AutofillReport(
        ok=True,
        phase="pickup",
        order_id=order_id,
        order_no=order.get("order_no", "") or "",
        token=payload.token,
        add_url=payload.add_url,
        pickup_count=result.get("pickup_count", pickup_count),
        detail=result.get("detail", ""),
    )


def et818_autofill_validate(order_id: int, payload: Et818AutofillActionPayload) -> Et818AutofillReport:
    order = get_order_or_raise(order_id)
    payload_response = build_et818_payload_response(order_id)
    et = payload_response.et818_payload
    result = bridge.eval(
        f'''(() => new Promise(resolve => setTimeout(() => {{
          try {{
            const token = {json.dumps(payload.token, ensure_ascii=False)};
            const expected = {json.dumps({
                'template_name': et.template_selection.template_name,
                'template_keyword': et.template_selection.template_keyword,
                'transport_name': et.order_info.transport_name,
                'team_category': et.order_info.team_category,
                'contact_name': et.order_info.contact_name,
                'order_no': et.order_info.order_no,
                'adult_count': et.order_info.adult_count,
                'child_count': et.order_info.child_count,
                'traveller_count': len(et.travellers),
                'pickup_count': len(et.pickup_dropoff),
            }, ensure_ascii=False)};
            const frame = [...document.querySelectorAll('iframe')].find(f => (f.src || '').includes(`time=${{token}}`));
            if (!frame || !frame.contentWindow) return resolve({{ ok:false, detail:'未找到 validate iframe' }});
            const win = frame.contentWindow;
            const doc = win.document;
            const mainTable = [...doc.querySelectorAll('table')].find(t => (t.innerText || '').includes('订单电话/姓名') && (t.innerText || '').includes('团队类别'));
            if (!mainTable) return resolve({{ ok:false, detail:'未找到 validate 主表' }});

            const getValueCellByLabel = (label) => {{
              for (const row of [...mainTable.rows]) {{
                for (let i = 0; i < row.cells.length; i += 1) {{
                  if ((row.cells[i].innerText || '').trim() === label) return row.cells[i + 1] || null;
                }}
              }}
              return null;
            }};
            const readDate = (cell) => {{
              const inputs = cell ? [...cell.querySelectorAll('input')] : [];
              return [inputs[0]?.value || '', inputs[1]?.value || '', inputs[2]?.value || ''].filter(Boolean).join('-');
            }};
            const peopleCell = getValueCellByLabel('参团人数: *');
            const peopleInputs = peopleCell ? [...peopleCell.querySelectorAll('input')] : [];

            const templateInput = (() => {{
              for (const row of [...mainTable.rows]) {{
                for (let i = 0; i < row.cells.length; i += 1) {{
                  if ((row.cells[i].innerText || '').trim() === '线路模板:') return row.cells[i + 1]?.querySelector('input') || null;
                }}
              }}
              return null;
            }})();

            const guestTables = [...doc.querySelectorAll('#guestTable')];
            const bodyTable = guestTables.find(t => t.querySelector('tbody'));
            const guestRows = bodyTable ? [...bodyTable.querySelectorAll('tbody tr')] : [];

            const pickupTable = [...doc.querySelectorAll('table')].find(t => (t.innerText || '').includes('班次时间') && (t.innerText || '').includes('接送描述'));
            const pickupRows = pickupTable ? [...pickupTable.querySelectorAll('tr')] : [];
            const pickupMainRows = [pickupRows[1], pickupRows[4]].filter(Boolean);

            const checks = [
              {{ field:'线路模板', mode:'输入后点选', expected: expected.template_name || expected.template_keyword, actual: templateInput?.value || '', ok: (templateInput?.value || '').includes(expected.template_keyword || '') }},
              {{ field:'大交通', mode:'下拉选择', expected: expected.transport_name, actual: getValueCellByLabel('大交通: *')?.querySelector('input')?.value || '' }},
              {{ field:'团队类别', mode:'下拉选择', expected: expected.team_category, actual: getValueCellByLabel('团队类别:')?.querySelector('input')?.value || '' }},
              {{ field:'订单电话/姓名', mode:'直接输入', expected: expected.contact_name, actual: getValueCellByLabel('订单电话/姓名:')?.querySelector('input')?.value || '' }},
              {{ field:'订单号', mode:'直接输入', expected: expected.order_no, actual: getValueCellByLabel('订单号:')?.querySelector('input')?.value || '' }},
              {{ field:'出团日期', mode:'直接输入', expected: '', actual: readDate(getValueCellByLabel('出团日期:')) }},
              {{ field:'返程日期', mode:'直接输入', expected: '', actual: readDate(getValueCellByLabel('返程日期:')) }},
              {{ field:'参团人数', mode:'直接输入', expected: `${{expected.adult_count}}/${{expected.child_count}}`, actual: peopleInputs.map(x => x.value || '').join('/') }},
              {{ field:'客人名单', mode:'表格输入', expected: String(expected.traveller_count), actual: String(guestRows.length), ok: guestRows.length >= expected.traveller_count }},
              {{ field:'接送信息', mode:'表格输入', expected: String(expected.pickup_count), actual: String(pickupMainRows.length), ok: pickupMainRows.length >= expected.pickup_count }},
            ].map(item => ('ok' in item ? item : {{ ...item, ok: !item.expected || item.actual.includes(item.expected) || item.actual === item.expected }}));

            const failed = checks.filter(x => !x.ok).map(x => `${{x.field}}: expected=${{x.expected}} actual=${{x.actual}}`);
            resolve({{
              ok:true,
              detail: failed.length ? 'validate 发现未提交字段' : 'validate 通过',
              checks,
              errors: failed,
              traveller_count: guestRows.length,
              pickup_count: pickupMainRows.length,
            }});
          }} catch (e) {{
            resolve({{ ok:false, detail:e.message || String(e) }});
          }}
        }}, 400)))()''',
        timeout=60,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise Et818BridgeError((result or {}).get("detail") or "validate 失败")
    return Et818AutofillReport(
        ok=True,
        phase="validate",
        order_id=order_id,
        order_no=order.get("order_no", "") or "",
        token=payload.token,
        add_url=payload.add_url,
        traveller_count=int(result.get("traveller_count") or 0),
        pickup_count=int(result.get("pickup_count") or 0),
        required_main=result.get("checks", []),
        errors=result.get("errors", []),
        detail=result.get("detail", ""),
    )


def et818_autofill_no_save(order_id: int) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    prepare_report = et818_autofill_prepare(order_id)
    payload = Et818AutofillActionPayload(token=prepare_report.token, add_url=prepare_report.add_url)
    steps.append({"phase": "prepare", "ok": prepare_report.ok, "detail": prepare_report.detail})

    template_report = et818_autofill_template(order_id, payload)
    steps.append({"phase": "template", "ok": template_report.ok, "detail": template_report.detail, "errors": template_report.errors})

    main_report = et818_autofill_main(order_id, payload)
    steps.append({"phase": "main", "ok": main_report.ok, "detail": main_report.detail, "errors": main_report.errors})

    travellers_report = None
    payload_response = build_et818_payload_response(order_id)
    if payload_response.et818_payload.travellers:
        travellers_report = et818_autofill_travellers(order_id, payload)
        steps.append({"phase": "travellers", "ok": travellers_report.ok, "detail": travellers_report.detail, "errors": travellers_report.errors})
    else:
        steps.append({"phase": "travellers", "ok": True, "detail": "无客人数据，跳过", "errors": []})

    pickup_report = None
    if payload_response.et818_payload.pickup_dropoff:
        pickup_report = et818_autofill_pickup(order_id, payload)
        steps.append({"phase": "pickup", "ok": pickup_report.ok, "detail": pickup_report.detail, "errors": pickup_report.errors})
    else:
        steps.append({"phase": "pickup", "ok": True, "detail": "无接送数据，跳过", "errors": []})

    validate_report = et818_autofill_validate(order_id, payload)
    steps.append({"phase": "validate", "ok": validate_report.ok and not validate_report.errors, "detail": validate_report.detail, "errors": validate_report.errors})

    return {
        "ok": True,
        "phase": "no_save",
        "order_id": order_id,
        "order_no": prepare_report.order_no,
        "token": prepare_report.token,
        "add_url": prepare_report.add_url,
        "steps": steps,
        "validate": validate_report.model_dump(),
    }


def et818_submit_after_confirm(order_id: int) -> dict[str, Any]:
    no_save = et818_autofill_no_save(order_id)
    validate = no_save.get("validate", {}) or {}
    errors = validate.get("errors", []) or []
    if errors:
        raise Et818BridgeError("validate 未通过，禁止提交")

    token = no_save.get("token", "") or ""
    add_url = no_save.get("add_url", "") or ""
    if not token:
        raise Et818BridgeError("缺少 token，无法提交")

    result = bridge.eval(
        f'''(() => new Promise(resolve => setTimeout(() => {{
          try {{
            const token = {json.dumps(token, ensure_ascii=False)};
            const frame = [...document.querySelectorAll('iframe')].find(f => (f.src || '').includes(`time=${{token}}`));
            if (!frame || !frame.contentWindow) return resolve({{ ok:false, detail:'未找到提交页 iframe' }});
            const doc = frame.contentWindow.document;
            const buttons = [...doc.querySelectorAll('button, input[type="button"], a')];
            const saveBtn = buttons.find(el => /保存(并新增)?/.test((el.innerText || el.value || el.textContent || '').trim()));
            if (!saveBtn) return resolve({{ ok:false, detail:'未找到保存按钮' }});
            saveBtn.click();
            resolve({{ ok:true, detail:'已点击保存按钮' }});
          }} catch (e) {{
            resolve({{ ok:false, detail:e.message || String(e) }});
          }}
        }}, 300)))()''',
        timeout=40,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise Et818BridgeError((result or {}).get("detail") or "提交失败")

    return {
        "ok": True,
        "phase": "submit",
        "order_id": order_id,
        "order_no": no_save.get("order_no", ""),
        "token": token,
        "add_url": add_url,
        "detail": result.get("detail", "已点击保存按钮"),
        "validate": validate,
    }


def _extract_total_from_raw_json(raw_json: str) -> str:
    """从 VBK 详情页原始文本中提取"总计"金额"""
    import re, json as _json
    if not raw_json:
        return ""
    try:
        parsed = _json.loads(raw_json)
        body = parsed.get("bodyText", "") or ""
    except Exception:
        body = raw_json
    m = re.search(r"总计[:：]?\s*([\d,]+\.?\d*)\s*(?:CNY|元|￥|¥)?", body)
    if m:
        return m.group(1)
    # 占位单时可能是"订单总额"
    m = re.search(r"订单总额[\s\S]*?(\d[\d,]+\.\d{2})", body)
    return m.group(1) if m else ""


def build_et818_payload_response(order_id: int) -> Et818PayloadResponse:
    order = get_order_or_raise(order_id)

    bundle = load_et818_source_bundle(order)

    template_selection = build_template_selection(bundle)
    order_info = build_order_info(bundle)
    travellers = build_travellers(bundle)
    pickup_dropoff = build_pickup_dropoff(bundle)
    notes = build_notes_block(bundle)
    meta = build_meta_block(bundle, template_selection, travellers, pickup_dropoff)

    payload = Et818Payload(
        template_selection=template_selection,
        order_info=order_info,
        travellers=travellers,
        pickup_dropoff=pickup_dropoff,
        notes=notes,
        meta=meta,
    )

    return Et818PayloadResponse(
        ok=True,
        source_platform=order.get("source_platform", "VBK") or "VBK",
        order_id=order_id,
        order_no=order.get("order_no", "") or "",
        total_amount_vbk=_extract_total_from_raw_json(bundle.get("vbk_detail", {}).get("raw_json", "") or ""),
        et818_payload=payload,
    )


def sync_vbk_detail_to_local(order_id: int, detail_data: dict[str, Any]) -> dict[str, Any]:
    order = get_order_or_raise(order_id)
    order_no = (order.get("order_no") or "").strip()
    if not order_no:
        raise HTTPException(status_code=400, detail="订单缺少 order_no")

    snapshot_payload = {
        "order_type_text": detail_data.get("order_type_text") or "",
        "confirm_status_text": detail_data.get("confirm_status_text") or "",
        "payment_status_text": detail_data.get("payment_status_text") or "",
        "departure_date": detail_data.get("departure_date") or order.get("departure_date") or "",
        "return_date": detail_data.get("return_date") or order.get("return_date") or "",
        "departure_city": detail_data.get("departure_city") or "",
        "customer_name": detail_data.get("customer_name") or order.get("customer_name") or "",
        "customer_phone": detail_data.get("customer_phone") or order.get("customer_phone") or "",
        "distribution_channel": detail_data.get("distribution_channel") or order.get("channel") or "",
        "scenic_booking_no": detail_data.get("scenic_booking_no") or "",
        "reservation_scenic_name": detail_data.get("reservation_scenic_name") or "",
        "merchant_note": detail_data.get("merchant_note") or "",
        "raw_json": detail_data.get("raw_json") or "",
    }
    upsert_vbk_detail_snapshot(order_no, snapshot_payload)

    travellers = normalize_travellers_for_storage(detail_data.get("travellers", []) or [])
    replace_order_travellers(order_id, travellers)

    pickup_dropoff = normalize_pickup_dropoff_for_storage(detail_data.get("pickup_dropoff", []) or [])
    replace_order_pickup_dropoff(order_id, pickup_dropoff)

    # ── 回写 orders 表 ──
    order_fields = {}
    supplier_name = detail_data.get("supplier_product_name") or ""
    if supplier_name:
        order_fields["route_name"] = supplier_name
    prod_id = detail_data.get("product_id") or ""
    if prod_id:
        order_fields["product_id"] = prod_id
    total_amt = detail_data.get("total_amount")
    if total_amt is not None:
        order_fields["total_amount"] = total_amt
    paid_amt = detail_data.get("paid_amount")
    if paid_amt is not None:
        order_fields["paid_amount"] = paid_amt
    adult_cnt = detail_data.get("adult_count")
    if adult_cnt is not None and adult_cnt > 0:
        order_fields["adult_count"] = adult_cnt
    child_cnt = detail_data.get("child_count")
    if child_cnt is not None:
        order_fields["child_count"] = child_cnt
    room_cnt = detail_data.get("room_count")
    if room_cnt is not None:
        order_fields["room_count"] = room_cnt
    room_sharing = detail_data.get("room_sharing") or ""
    # 拼房信息已存入 vbk_detail_snapshot 的 raw_json，无需额外存储

    if order_fields:
        from database import db_cursor, utc_now_str
        sets = ", ".join(f"{k} = ?" for k in order_fields)
        vals = list(order_fields.values())
        with db_cursor() as conn:
            conn.execute(f"UPDATE orders SET {sets}, updated_at = ? WHERE id = ?", vals + [utc_now_str(), order_id])

    return {
        "ok": True,
        "order_id": order_id,
        "order_no": order_no,
        "traveller_count": len(travellers),
        "pickup_dropoff_count": len(pickup_dropoff),
        "snapshot_updated": True,
        "orders_updated": list(order_fields.keys()),
    }


def load_et818_source_bundle(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "order": order,
        "order_notes": load_order_notes(order["id"]),
        "travellers": load_order_travellers(order["id"]),
        "pickup_dropoff": load_order_pickup_dropoff(order["id"]),
        "vbk_detail": load_vbk_detail_snapshot(order),
        "manual_overrides": load_manual_et818_overrides(order["id"]),
    }


def build_template_selection(bundle: dict[str, Any]) -> TemplateSelection:
    order = bundle["order"]
    vbk_detail = bundle.get("vbk_detail", {})

    supplier_name = (order.get("route_name", "") or "").strip()
    product_name = (order.get("product_name", "") or "").strip()
    departure_city = choose_first_nonempty(vbk_detail.get("departure_city"), "")
    channel = normalize_et818_channel_name(order.get("channel", "") or "")

    match = match_et818_template(
        supplier_product_name=supplier_name,
        product_name=product_name,
        departure_city=departure_city,
        channel=channel,
    )

    confidence = float(match.get("match_confidence", 0.0) or 0.0)
    template_keyword = (match.get("template_keyword", "") or "").strip()
    if not template_keyword:
        source = supplier_name or product_name
        template_keyword = source[:4] if source else ""

    return TemplateSelection(
        template_name=match.get("template_name", "") or "",
        template_keyword=template_keyword,
        match_confidence=confidence,
        match_basis=TemplateMatchBasis(
            supplier_product_name=supplier_name,
            product_name=product_name,
            departure_city=departure_city,
            channel=channel,
        ),
        needs_manual_confirm=confidence < 0.8,
    )


def build_order_info(bundle: dict[str, Any]) -> OrderInfo:
    order = bundle["order"]
    vbk_detail = bundle.get("vbk_detail", {})
    overrides = bundle.get("manual_overrides", {}) or {}

    departure_date = choose_first_nonempty(
        overrides.get("departure_date"),
        order.get("departure_date"),
        vbk_detail.get("departure_date"),
        "",
    )

    return_date = choose_first_nonempty(
        overrides.get("return_date"),
        order.get("return_date"),
        vbk_detail.get("return_date"),
        "",
    )

    visit_date = choose_first_nonempty(
        overrides.get("visit_date"),
        vbk_detail.get("visit_date"),
        departure_date,
        "",
    )

    adult_count = safe_int(order.get("adult_count"), default=0)
    child_count = safe_int(order.get("child_count"), default=0)

    return OrderInfo(
        adult_count=adult_count,
        child_count=child_count,
        channel_name=normalize_et818_channel_name(order.get("channel", "") or ""),
        contact_name=choose_first_nonempty(
            overrides.get("contact_name"),
            order.get("customer_name"),
            vbk_detail.get("customer_name"),
            "",
        ),
        contact_phone=choose_first_nonempty(
            overrides.get("contact_phone"),
            order.get("customer_phone"),
            vbk_detail.get("customer_phone"),
            "",
        ),
        departure_date=split_date_parts(departure_date),
        return_date=split_date_parts(return_date),
        visit_date=split_date_parts(visit_date) if visit_date else None,
        transport_name=infer_transport_name(bundle),
        order_no=(order.get("order_no", "") or "").strip(),
        sales_name=choose_first_nonempty(
            overrides.get("sales_name"),
            "李明强",
        ),
        after_sales_name=choose_first_nonempty(
            overrides.get("after_sales_name"),
            "",
        ),
        team_category=infer_team_category(bundle),
        scenic_booking_no=choose_first_nonempty(
            overrides.get("scenic_booking_no"),
            vbk_detail.get("scenic_booking_no"),
            "",
        ),
        reservation_scenic_name=choose_first_nonempty(
            overrides.get("reservation_scenic_name"),
            vbk_detail.get("reservation_scenic_name"),
            "无需预约",
        ),
        room_need=build_room_need(bundle),
    )


def build_travellers(bundle: dict[str, Any]) -> list[Traveller]:
    raw_items = bundle.get("travellers", []) or []
    out: list[Traveller] = []

    for item in raw_items:
        birth_raw = choose_first_nonempty(item.get("birth_date"), "")
        age = item.get("age")
        if not age and birth_raw:
            age = calc_age_from_birth_date(birth_raw)

        out.append(
            Traveller(
                name=(item.get("name", "") or "").strip(),
                phone=(item.get("phone", "") or "").strip(),
                id_type=normalize_id_type(item.get("id_type", "") or ""),
                id_no=(item.get("id_no", "") or "").strip(),
                gender=normalize_gender(item.get("gender", "") or ""),
                birth_date=split_date_parts(birth_raw),
                age=safe_int(age, default=0),
                person_type=normalize_person_type(item.get("person_type", "成人") or "成人"),
                native_place=(item.get("native_place", "") or "").strip(),
                note=(item.get("note", "") or "").strip(),
                source=TravellerSource(
                    encrypted_info_revealed=bool(item.get("encrypted_info_revealed", True)),
                    from_vbk_detail=bool(item.get("from_vbk_detail", True)),
                ),
            )
        )

    return out


def build_pickup_dropoff(bundle: dict[str, Any]) -> list[PickupDropoffItem]:
    raw_items = bundle.get("pickup_dropoff", []) or []
    out: list[PickupDropoffItem] = []

    for item in raw_items:
        out.append(
            PickupDropoffItem(
                action=normalize_pickup_action(item.get("action", "") or ""),
                date=split_date_parts(item.get("date", "") or ""),
                location=normalize_pickup_location(item.get("location", "") or ""),
                flight_no=(item.get("flight_no", "") or "").strip(),
                time=(item.get("time", "") or "").strip(),
                description=(item.get("description", "") or "").strip(),
                vehicle_company=(item.get("vehicle_company", "") or "").strip(),
                driver_name=(item.get("driver_name", "") or "").strip(),
                project_name=(item.get("project_name", "") or "").strip(),
                enabled=bool(item.get("enabled", True)),
            )
        )

    return out


def build_notes_block(bundle: dict[str, Any]) -> NotesBlock:
    notes = bundle.get("order_notes", {}) or {}

    order_note = (notes.get("order_note", "") or "").strip()
    merchant_note = (notes.get("merchant_note", "") or "").strip()
    internal_note = (notes.get("internal_note", "") or "").strip()

    merged = " | ".join([x for x in [order_note, merchant_note, internal_note] if x])

    return NotesBlock(
        order_note=order_note,
        merchant_note=merchant_note,
        internal_note=internal_note,
        merged_note=merged,
    )


def build_meta_block(
    bundle: dict[str, Any],
    template_selection: TemplateSelection,
    travellers: list[Traveller],
    pickup_dropoff: list[PickupDropoffItem],
) -> MetaBlock:
    order = bundle["order"]
    vbk_detail = bundle.get("vbk_detail", {}) or {}

    warnings: list[str] = []
    missing_fields: list[str] = []

    if not template_selection.template_name:
        warnings.append("未匹配到明确的 ET818 线路模板")
        missing_fields.append("template_selection.template_name")

    if not travellers:
        warnings.append("缺少客人明细，ET818 客人名单无法完整自动填充")
        missing_fields.append("travellers")

    if not (order.get("customer_name") or "").strip():
        missing_fields.append("order_info.contact_name")

    if not (order.get("order_no") or "").strip():
        missing_fields.append("order_info.order_no")

    if not (order.get("departure_date") or vbk_detail.get("departure_date") or "").strip():
        missing_fields.append("order_info.departure_date")

    manual_review_recommended = bool(
        template_selection.needs_manual_confirm or not travellers
    )

    return MetaBlock(
        autofill_priority=[
            "template_selection",
            "order_info",
            "travellers",
            "pickup_dropoff",
            "notes",
        ],
        template_autofill_expected=TemplateAutofillExpected(
            product_section=True,
            ticket_section=True,
            room_section=True,
        ),
        manual_review_recommended=manual_review_recommended,
        warnings=warnings,
        missing_fields=missing_fields,
        debug=MetaDebug(
            vbk_order_type=(vbk_detail.get("order_type_text", "") or "").strip(),
            vbk_confirm_status=(order.get("order_status", "") or "").strip(),
            vbk_payment_status=(order.get("payment_status", "") or "").strip(),
        ),
    )


def match_et818_template(
    supplier_product_name: str,
    product_name: str,
    departure_city: str,
    channel: str,
) -> dict[str, Any]:
    text = f"{supplier_product_name} {product_name}".strip()
    supplier_prefix = (supplier_product_name or "").strip()[:4]

    if "甘南" in text and "莲宝" in text and "7" in text and "自营" in text:
        return {
            "template_name": "甘南莲宝7天[携程自营]",
            "template_keyword": supplier_prefix or "甘南",
            "match_confidence": 0.93,
        }

    if "甘南" in text and "莲宝" in text and "7" in text:
        return {
            "template_name": "甘南莲宝7天",
            "template_keyword": supplier_prefix or "甘南",
            "match_confidence": 0.85,
        }

    if "甘南" in text and "莲宝" in text and "5" in text:
        return {
            "template_name": "甘南莲宝5日",
            "template_keyword": supplier_prefix or "甘南",
            "match_confidence": 0.85,
        }

    return {
        "template_name": "",
        "template_keyword": supplier_prefix or (product_name[:4] if product_name else ""),
        "match_confidence": 0.3,
    }


def normalize_et818_channel_name(channel: str) -> str:
    c = (channel or "").strip()

    if c in ("携程", "携程门店", "自营", "携程用户"):
        return "携程83"
    if c == "CTripShop":
        return "携程83"
    if c == "去哪儿":
        return "去哪儿"
    if c == "同程":
        return "同程门店"
    if c == "飞猪":
        return "飞猪"

    return c or "携程83"


def infer_transport_name(bundle: dict[str, Any]) -> str:
    vbk_detail = bundle.get("vbk_detail", {}) or {}
    flights = vbk_detail.get("flights", []) or []

    if len(flights) >= 2:
        return "双飞"
    if len(flights) == 1:
        return "单飞"

    text = " ".join([
        str(bundle["order"].get("product_name", "") or ""),
        str(bundle["order"].get("route_name", "") or ""),
    ])

    if "双飞" in text:
        return "双飞"
    if "单飞" in text:
        return "单飞"
    if "双动" in text:
        return "双动"
    if "单动" in text:
        return "单动"
    if "汽车" in text:
        return "汽车"

    return "当地参"


def infer_team_category(bundle: dict[str, Any]) -> str:
    travellers = bundle.get("travellers", []) or []
    has_child = any((t.get("person_type", "") or "") == "儿童" for t in travellers)

    if has_child:
        return "亲子团"

    ages = [safe_int(t.get("age"), default=0) for t in travellers if t.get("age")]
    if ages and min(ages) >= 50:
        return "老友团"

    return "快拼团"


def build_room_need(bundle: dict[str, Any]) -> RoomNeed:
    order = bundle["order"]
    travellers = bundle.get("travellers", []) or []

    adult = safe_int(order.get("adult_count"), default=0)
    child = safe_int(order.get("child_count"), default=0)

    departure = choose_first_nonempty(order.get("departure_date"), "")
    ret = choose_first_nonempty(order.get("return_date"), "")
    nights = calc_nights(departure, ret)

    standard = 0
    big_bed = 0
    triple = 0
    single_female = 0
    single_male = 0

    if adult == 2 and child == 0:
        standard = 1
    elif adult == 1:
        first_gender = normalize_gender((travellers[0].get("gender", "") if travellers else ""))
        if first_gender == "女":
            single_female = 1
        else:
            single_male = 1
    elif adult == 3:
        triple = 1
    elif adult == 4:
        standard = 2
    elif adult > 0:
        standard = max(1, adult // 2)

    return RoomNeed(
        standard=standard,
        big_bed=big_bed,
        triple=triple,
        single_female=single_female,
        single_male=single_male,
        nights=nights,
    )


def normalize_id_type(id_type: str) -> str:
    t = (id_type or "").strip()
    mapping = {
        "身份证": "身份证",
        "护照": "护照",
        "港澳通行证": "港澳通行证",
        "台胞证": "台胞证",
        "回乡证": "回乡证",
        "军人证": "军人证",
        "驾驶证": "驾驶证",
        "学生证": "学生证",
    }
    return mapping.get(t, t or "身份证")


def normalize_gender(gender: str) -> str:
    g = (gender or "").strip()
    mapping = {
        "男": "男",
        "女": "女",
        "M": "男",
        "F": "女",
        "male": "男",
        "female": "女",
    }
    return mapping.get(g, g or "男")


def normalize_person_type(person_type: str) -> str:
    p = (person_type or "").strip()
    if p in ("儿童", "小童", "child"):
        return "儿童"
    return "成人"


def normalize_pickup_action(action: str) -> str:
    a = (action or "").strip()
    if a in ("接机", "接站", "1接机/站"):
        return "1接机/站"
    if a in ("送机", "送站", "2送机/站"):
        return "2送机/站"
    return a


def normalize_pickup_location(location: str) -> str:
    return (location or "").strip()


def normalize_travellers_for_storage(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(items):
        birth_date = choose_first_nonempty(item.get("birth_date"), item.get("birthday"), "")
        age = safe_int(item.get("age"), default=0)
        if not age and birth_date:
            age = calc_age_from_birth_date(birth_date)
        out.append(
            {
                "name": (item.get("name") or "").strip(),
                "phone": (item.get("phone") or "").strip(),
                "id_type": normalize_id_type(item.get("id_type") or item.get("card_type") or "身份证"),
                "id_no": (item.get("id_no") or item.get("card_no") or "").strip(),
                "gender": normalize_gender(item.get("gender") or ""),
                "birth_date": birth_date,
                "age": age,
                "person_type": normalize_person_type(item.get("person_type") or item.get("type") or "成人"),
                "native_place": (item.get("native_place") or item.get("address") or "").strip(),
                "note": (item.get("note") or "").strip(),
                "encrypted_info_revealed": bool(item.get("encrypted_info_revealed", True)),
                "from_vbk_detail": bool(item.get("from_vbk_detail", True)),
                "sort_index": int(item.get("sort_index", idx) or idx),
            }
        )
    return out


def normalize_pickup_dropoff_for_storage(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(items):
        out.append(
            {
                "action": normalize_pickup_action(item.get("action") or ""),
                "date": choose_first_nonempty(item.get("date"), ""),
                "location": normalize_pickup_location(item.get("location") or item.get("departure") or ""),
                "flight_no": (item.get("flight_no") or item.get("train_no") or "").strip(),
                "time": (item.get("time") or item.get("time_text") or "").strip(),
                "description": (item.get("description") or "").strip(),
                "vehicle_company": (item.get("vehicle_company") or "").strip(),
                "driver_name": (item.get("driver_name") or "").strip(),
                "project_name": (item.get("project_name") or "").strip(),
                "enabled": bool(item.get("enabled", True)),
                "sort_index": int(item.get("sort_index", idx) or idx),
            }
        )
    return out


def split_date_parts(raw: str | None) -> DateParts:
    raw = (raw or "").strip()
    if not raw:
        return DateParts(raw="", year="", month="", day="")

    parts = raw.split("-")
    if len(parts) != 3:
        return DateParts(raw=raw, year="", month="", day="")

    return DateParts(
        raw=raw,
        year=parts[0],
        month=parts[1],
        day=parts[2],
    )


def calc_nights(departure_date: str, return_date: str) -> int:
    try:
        d1 = datetime.strptime(departure_date, "%Y-%m-%d")
        d2 = datetime.strptime(return_date, "%Y-%m-%d")
        return max(0, (d2 - d1).days)
    except Exception:
        return 0


def calc_age_from_birth_date(birth_date: str) -> int:
    try:
        b = datetime.strptime(birth_date, "%Y-%m-%d")
        today = datetime.today()
        age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
        return max(age, 0)
    except Exception:
        return 0


def choose_first_nonempty(*values: Any) -> str:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def get_order_or_raise(order_id: int) -> dict[str, Any]:
    order = find_order_by_id_adapter(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


def load_order_notes(order_id: int) -> dict[str, Any]:
    order = find_order_by_id_adapter(order_id) or {}
    snapshot = get_vbk_detail_snapshot(order.get("order_no", "") or "") or {}
    return {
        "order_note": (order.get("customer_note", "") or "").strip(),
        "merchant_note": (snapshot.get("merchant_note", "") or "").strip(),
        "internal_note": "",
    }


def load_order_travellers(order_id: int) -> list[dict[str, Any]]:
    items = find_order_travellers_adapter(order_id)
    return items or []


def load_order_pickup_dropoff(order_id: int) -> list[dict[str, Any]]:
    items = find_pickup_dropoff_adapter(order_id)
    return items or []


def load_vbk_detail_snapshot(order: dict[str, Any]) -> dict[str, Any]:
    snapshot = find_vbk_detail_by_order_no_adapter(order.get("order_no", "") or "")
    return snapshot or {}


def load_manual_et818_overrides(order_id: int) -> dict[str, Any]:
    return {}


def find_order_by_id_adapter(order_id: int) -> dict[str, Any] | None:
    try:
        return get_order(order_id)
    except Exception:
        return None


def find_order_travellers_adapter(order_id: int) -> list[dict[str, Any]]:
    try:
        return list_order_travellers(order_id)
    except Exception:
        return []


def find_pickup_dropoff_adapter(order_id: int) -> list[dict[str, Any]]:
    try:
        return list_order_pickup_dropoff(order_id)
    except Exception:
        return []


def find_vbk_detail_by_order_no_adapter(order_no: str) -> dict[str, Any] | None:
    try:
        return get_vbk_detail_snapshot(order_no)
    except Exception:
        return None
