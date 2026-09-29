"""Web 界面：stdlib http.server，无第三方依赖。

页面：楼栋日历 / 材料清单 / 保护点位确认 / 车辆预约 / 门岗核验 / 签收差异。
配色：白色底、深森林绿导航与状态、工程橙用于操作与超限警示。
"""
import html
import json
import mimetypes
import urllib.parse
from datetime import date as _date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, photos, services
from .adapters import VehicleAdapter, WeighbridgeAdapter
from .db import connect, init_db
from .seed import seed

ESC = html.escape

STATUS_CN = {
    "held": ("占位中·待物业确认", "#b3541e"),
    "confirmed": ("保护已确认·待放行", config.COLOR_FOREST),
    "gate_passed": ("门岗已放行", "#1d6fa5"),
    "arrived": ("已到场", "#1d6fa5"),
    "signed": ("已签收", "#555b58"),
    "released": ("占位已释放", "#8a8f8c"),
}

ISSUE_CN = {
    "late": "车辆迟到", "overweight": "材料超重", "no_protection": "保护板缺失",
    "elevator_fault": "电梯故障", "wrong_building": "错楼栋",
    "already_passed": "已放行", "concurrent_lost": "并发冲突",
    "weighbridge_fault": "地磅故障", "not_confirmed": "未确认", "none": "放行",
}

vehicles = VehicleAdapter()
# 门岗演示用称重适配器：jitter 模式；超重演练在 URL 加 ?wb=overweight
weighbridges = {
    "exact": WeighbridgeAdapter("exact"),
    "jitter": WeighbridgeAdapter("jitter", seed=7),
    "overweight": WeighbridgeAdapter("overweight", force_extra_kg=120),
}


# ---------------------------------------------------------------- 样式

CSS = f"""
:root {{
  --forest:{config.COLOR_FOREST}; --forest-d:{config.COLOR_FOREST_DARK};
  --orange:{config.COLOR_ORANGE}; --ink:{config.COLOR_INK}; --mist:{config.COLOR_MIST};
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  color:var(--ink); background:#fff; }}
header {{ background:var(--forest); color:#fff; padding:14px 26px; display:flex;
  align-items:baseline; gap:22px; flex-wrap:wrap; }}
header h1 {{ font-size:19px; margin:0; letter-spacing:1px; }}
nav a {{ color:#cfe3d8; text-decoration:none; margin-right:16px; font-size:14px; }}
nav a.on,nav a:hover {{ color:#fff; border-bottom:2px solid var(--orange); padding-bottom:3px; }}
main {{ max-width:1180px; margin:0 auto; padding:22px 26px 60px; }}
h2 {{ color:var(--forest); border-left:5px solid var(--orange); padding-left:10px; font-size:18px; }}
h3 {{ color:var(--forest-d); font-size:15px; margin:18px 0 8px; }}
table {{ border-collapse:collapse; width:100%; background:#fff; font-size:13.5px; }}
th,td {{ border:1px solid #d9e2dc; padding:7px 9px; text-align:left; vertical-align:top; }}
th {{ background:var(--forest); color:#fff; font-weight:600; }}
tr:nth-child(even) td {{ background:#f7faf8; }}
.card {{ border:1px solid #d9e2dc; border-radius:10px; padding:16px 18px; margin:14px 0;
  box-shadow:0 1px 3px rgba(20,67,47,.06); }}
.grid {{ display:grid; gap:14px; }}
.btn {{ display:inline-block; background:var(--orange); color:#fff; border:0; border-radius:6px;
  padding:8px 18px; font-size:14px; cursor:pointer; text-decoration:none; }}
.btn.sec {{ background:#fff; color:var(--forest); border:1.5px solid var(--forest); }}
.btn.big {{ font-size:22px; padding:14px 44px; border-radius:10px; }}
.btn:disabled {{ background:#bbb; cursor:not-allowed; }}
input,select,textarea {{ font-size:14px; padding:7px 9px; border:1px solid #b9c6bf;
  border-radius:6px; background:#fff; }}
label {{ font-size:13px; color:var(--forest-d); }}
.badge {{ display:inline-block; padding:2px 9px; border-radius:20px; color:#fff;
  font-size:12px; white-space:nowrap; }}
.tag {{ display:inline-block; padding:1px 8px; border-radius:4px; font-size:12px; }}
.tag.orange {{ background:var(--orange); color:#fff; }}
.tag.gray {{ background:#e4e9e6; color:#566; }}
.weight {{ font-size:20px; font-weight:800; color:var(--forest-d); font-variant-numeric:tabular-nums; }}
.over {{ color:#c62828; }}
.muted {{ color:#788079; font-size:12.5px; }}
.flash {{ background:#fff3ea; border-left:5px solid var(--orange); padding:10px 14px;
  margin:12px 0; border-radius:0 6px 6px 0; font-size:14px; }}
.okbox {{ background:#eef7f1; border-left:5px solid var(--forest); padding:10px 14px;
  margin:12px 0; border-radius:0 6px 6px 0; }}
/* 日历按电梯分栏 */
.cal-grid {{ display:grid; gap:12px; }}
.elev-col {{ border:1px solid #cdd8d2; border-radius:10px; overflow:hidden; }}
.elev-col > .head {{ background:var(--forest-d); color:#fff; padding:8px 12px;
  display:flex; justify-content:space-between; font-size:14px; }}
.slot {{ display:flex; justify-content:space-between; gap:8px; padding:7px 12px;
  border-top:1px solid #e3eae5; font-size:13px; align-items:center; }}
.slot:first-of-type {{ border-top:0; }}
.slot.empty {{ color:#aab4ae; }}
.slot.held {{ background:#fdf1e8; }}
.slot a {{ color:var(--forest); font-weight:600; text-decoration:none; }}
/* 门岗端 */
.gate-wrap {{ max-width:760px; margin:10px auto; }}
.gate-plate {{ font-size:76px; font-weight:900; letter-spacing:4px; color:var(--forest-d);
  text-align:center; margin:6px 0; font-variant-numeric:tabular-nums; }}
.gate-slot {{ font-size:40px; font-weight:800; text-align:center; color:var(--forest); }}
.gate-result {{ font-size:46px; font-weight:900; text-align:center; padding:26px;
  border-radius:14px; margin:14px 0; }}
.gate-result.pass {{ background:#eef7f1; color:var(--forest); border:3px solid var(--forest); }}
.gate-result.reject {{ background:#fdecec; color:#c62828; border:3px solid #c62828; }}
.gate-rows {{ font-size:24px; }}
.gate-rows td {{ padding:10px 14px; }}
.gate-input {{ font-size:34px; padding:12px 18px; width:100%; text-align:center;
  letter-spacing:3px; }}
.photo {{ width:150px; height:100px; object-fit:cover; border-radius:6px;
  border:1px solid #cdd8d2; }}
"""

NAV = [("/", "楼栋日历"), ("/booking", "车辆预约"), ("/materials", "材料清单"),
       ("/protection", "保护点位"), ("/gate", "门岗核验"), ("/signoffs", "签收差异")]


def page(title, active, body, flash=None):
    nav = "".join(
        f'<a href="{u}" class="{"on" if u == active else ""}">{n}</a>' for u, n in NAV)
    flash_html = f'<div class="flash">{ESC(flash)}</div>' if flash else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{ESC(title)}｜装修材料进场系统</title><style>{CSS}</style></head>
<body><header><h1>装修材料进场预约 · 电梯保护确认</h1><nav>{nav}</nav></header>
<main>{flash_html}{body}</main></body></html>"""


def badge(status):
    label, color = STATUS_CN.get(status, (status, "#777"))
    return f'<span class="badge" style="background:{color}">{ESC(label)}</span>'


def q(text, **kw):
    return ESC(str(text)).format(**{k: ESC(str(v)) for k, v in kw.items()})


# ---------------------------------------------------------------- 页面

def page_calendar(conn, params):
    d = params.get("date", [_date.today().isoformat()])[0]
    services.expire_holds(conn)
    data = services.calendar(conn, d)
    blocks = []
    for b in data["buildings"]:
        cols = []
        for col in b["elevators"]:
            e = col["elevator"]
            fault = ' <span class="tag orange">故障</span>' if e["status"] == "fault" else ""
            by_slot = {s["slot_start"]: s for s in col["slots"]}
            slot_html = []
            for start, end in data["slot_labels"]:
                s = by_slot.get(start)
                if s:
                    cls = "held" if s["status"] == "held" else ""
                    slot_html.append(
                        f'<div class="slot {cls}"><span><b>{start}</b> '
                        f'<a href="/appt?id={s["id"]}">{ESC(s["vehicle_plate"])}</a> '
                        f'{ESC(s["room_no"])}</span>'
                        f'<span>{badge(s["status"])}</span></div>')
                else:
                    slot_html.append(
                        f'<div class="slot empty"><span>{start}–{end}</span>'
                        f'<span>可约</span></div>')
            cols.append(
                f'<div class="elev-col"><div class="head"><span>{ESC(e["code"])}{fault}</span>'
                f'<span>载重 {e["load_kg"]}kg · {ESC(e["door_cm"])}门洞</span></div>'
                f'{"".join(slot_html)}</div>')
        blocks.append(
            f'<div class="card"><h3>{ESC(b["building"]["name"])}　'
            f'<span class="muted">{ESC(b["building"]["address"])}　'
            f'装卸区 {b["building"]["loading_bays"]} 车位</span></h3>'
            f'<div class="cal-grid grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">'
            f'{"".join(cols)}</div></div>')
    body = f"""
    <h2>楼栋日历 · {ESC(d)}</h2>
    <form method="get" action="/" style="margin:8px 0 16px">
      <input type="date" name="date" value="{ESC(d)}"> <button class="btn sec">切换日期</button>
      <a class="btn" href="/booking?date={ESC(d)}">新建预约</a>
    </form>
    <p class="muted">日历按楼栋 → 电梯分栏；占位 {config.HOLD_TTL_SECONDS // 60} 分钟内未完成物业保护确认将自动释放时段。</p>
    {''.join(blocks)}"""
    return page("楼栋日历", "/", body, params.get("msg", [None])[0])


def _unit_options(conn):
    rows = conn.execute(
        """SELECT u.id, b.name bn, u.room_no, u.resident FROM unit u
           JOIN building b ON b.id=u.building_id ORDER BY b.id, u.room_no""").fetchall()
    return rows


def page_booking(conn, params):
    services.expire_holds(conn)
    d = params.get("date", [_date.today().isoformat()])[0]
    units = _unit_options(conn)
    unit_opts = "".join(
        f'<option value="{r["id"]}">{ESC(r["bn"])} {ESC(r["room_no"])}'
        f'（{ESC(r["resident"])}）</option>' for r in units)
    elev_rows = conn.execute(
        "SELECT id, building_id, code, load_kg, status FROM elevator ORDER BY id").fetchall()
    elev_js = json.dumps([
        {"id": r["id"], "bid": r["building_id"], "code": r["code"],
         "load": r["load_kg"], "fault": r["status"] == "fault"} for r in elev_rows],
        ensure_ascii=False)
    slots = services.slot_labels(d)
    slot_opts = "".join(f'<option value="{s}">{s}–{e}</option>' for s, e in slots)
    plate_opts = "".join(f'<option value="{ESC(p)}">{ESC(p)}</option>'
                         for p in vehicles.all_plates())
    body = f"""
    <h2>车辆预约（创建时段占位）</h2>
    <div class="card"><form method="post" action="/book">
      <input type="hidden" name="date" value="{ESC(d)}">
      <p><label>装修单元</label><br>
        <select name="unit_id" id="unit" required>{unit_opts}</select></p>
      <p><label>电梯</label><br>
        <select name="elevator_id" id="elev" required></select>
        <span class="muted" id="einfo"></span></p>
      <p><label>进场时段</label><br><select name="slot_start">{slot_opts}</select></p>
      <p><label>车牌（车辆适配器）</label><br>
        <input name="plate" list="plates" required value="沪A·D3268" style="min-width:220px">
        <datalist id="plates">{plate_opts}</datalist></p>
      <p><label>备注</label><br><input name="note" style="width:360px"></p>
      <button class="btn">创建预约（占用时段）</button>
    </form></div>"""
    # 单元→楼栋映射由后端直接渲染
    ub = {r["id"]: conn.execute("SELECT building_id FROM unit WHERE id=?",
                                (r["id"],)).fetchone()["building_id"] for r in units}
    body += f"""
    <script>
    const ELEVS={elev_js};
    const UNIT_BID = {dict(ub)};
    const elevSel=document.getElementById('elev'), unitSel=document.getElementById('unit');
    const info=document.getElementById('einfo');
    function fillElev(){{
      const bid=UNIT_BID[unitSel.value];
      elevSel.innerHTML='';
      ELEVS.filter(e=>e.bid===bid).forEach(e=>{{
        const o=document.createElement('option');
        o.value=e.id; o.textContent=e.code+'（载重'+e.load+'kg）'+(e.fault?' 故障停用':'');
        o.disabled=e.fault; elevSel.appendChild(o);
      }});
      const cur=ELEVS.find(e=>e.id==elevSel.value);
      info.textContent=cur?('轿厢门洞 '+cur.code):'';
    }}
    unitSel.addEventListener('change',fillElev); fillElev();
    </script>
    <p class="muted">车辆与司机信息来自模拟车辆适配器；申报材料总重超出电梯载重将被拒绝，等于额定载重为边界放行。</p>"""
    return page("车辆预约", "/booking", body, params.get("msg", [None])[0])


def page_materials(conn, params):
    units = _unit_options(conn)
    rows = conn.execute(
        """SELECT m.*, b.name bn, u.room_no, e.load_kg FROM material m
           JOIN unit u ON u.id=m.unit_id JOIN building b ON b.id=u.building_id
           LEFT JOIN appointment a ON a.unit_id=u.id
           LEFT JOIN elevator e ON e.id=a.elevator_id
           GROUP BY m.id ORDER BY b.id, u.room_no, m.id""").fetchall()
    # 单元汇总（用默认货梯载重作为超限对照）
    load_by_unit = {}
    for r in conn.execute(
            """SELECT u.id, MAX(e.load_kg) load_kg FROM unit u
               JOIN elevator e ON e.building_id=u.building_id GROUP BY u.id"""):
        load_by_unit[r["id"]] = r["load_kg"]
    cards = []
    for un in units:
        mats = [r for r in rows if r["room_no"] == un["room_no"] and r["bn"] == un["bn"]]
        total = round(sum(m["weight_kg"] for m in mats), 1)
        load = load_by_unit.get(un["id"], 0)
        over = total > load
        trs = "".join(
            f'<tr><td>{ESC(m["name"])}</td>'
            f'<td class="weight {"over" if m["weight_kg"]>=500 else ""}">{m["weight_kg"]:g}</td>'
            f'<td>{m["length_cm"]:g}×{m["width_cm"]:g}×{m["height_cm"]:g} cm</td>'
            f'<td>{"<span class=tag orange>超长</span>" if m["overlong"] else "<span class=tag gray>常规</span>"}</td></tr>'
            for m in mats)
        cards.append(f"""
        <div class="card">
          <h3>{ESC(un["bn"])} {ESC(un["room_no"])}　<span class="muted">{ESC(un["resident"])}</span></h3>
          <p>合计重量：<span class="weight {"over" if over else ""}">{total:g} kg</span>
             {"<span class='tag orange'>超出货梯载重 "+str(load)+"kg</span>" if over else f'<span class="tag gray">货梯载重 {load:g}kg 内</span>'}
             {"<span class='tag orange'>含超长材料</span>" if any(m["overlong"] for m in mats) else ""}</p>
          <table><tr><th>材料</th><th>重量 kg</th><th>尺寸（长×宽×高）</th><th>超长</th></tr>{trs}</table>
        </div>""")
    body = f'<h2>材料清单</h2><p class="muted">粗体数字为材料重量；超过货梯载重或超长材料以工程橙标记。</p>{"".join(cards)}'
    return page("材料清单", "/materials", body, params.get("msg", [None])[0])


def page_appt(conn, appt_id, params):
    services.expire_holds(conn)
    appt = services.get_appointment(conn, appt_id)
    if not appt:
        return page("预约", "/", '<div class="flash">预约不存在</div>')
    b = conn.execute("SELECT * FROM building WHERE id=?", (appt["building_id"],)).fetchone()
    u = conn.execute("SELECT * FROM unit WHERE id=?", (appt["unit_id"],)).fetchone()
    e = conn.execute("SELECT * FROM elevator WHERE id=?", (appt["elevator_id"],)).fetchone()
    _, _, mats = services._unit_material_totals(conn, appt["unit_id"], appt_id)
    additions = {r["material_id"] for r in conn.execute(
        "SELECT material_id FROM material_addition WHERE appointment_id=?", (appt_id,))}
    mrows = "".join(
        f'<tr><td>{ESC(m["name"])}{" <span class=tag orange>临时新增</span>" if m["id"] in additions else ""}</td>'
        f'<td class="weight">{m["weight_kg"]:g}</td>'
        f'<td>{m["length_cm"]:g}×{m["width_cm"]:g}×{m["height_cm"]:g}</td>'
        f'<td>{"超长" if m["overlong"] else "常规"}</td></tr>' for m in mats)
    over = appt["total_weight_kg"] > e["load_kg"]

    # 保护点位巡检
    points = conn.execute(
        "SELECT * FROM protection_point WHERE building_id=? ORDER BY id",
        (appt["building_id"],)).fetchall()
    checks = {r["point_id"]: r for r in conn.execute(
        "SELECT * FROM protection_check WHERE appointment_id=?", (appt_id,))}
    pc = conn.execute("SELECT * FROM protection_confirmation WHERE appointment_id=?",
                      (appt_id,)).fetchone()
    prow = []
    can_check = appt["status"] in ("held", "confirmed")
    for p in points:
        c = checks.get(p["id"])
        present = c and c["present"]
        photo_img = f'<img class="photo" src="/{ESC(c["photo_path"])}">' if c and c["photo_path"] else '<span class="muted">无照片</span>'
        prow.append(f"""<tr>
          <td>{ESC(p["name"])}{" <span class='tag orange'>必设</span>" if p["required"] else ""}</td>
          <td>{'<span class="tag" style="background:var(--forest);color:#fff">已到位</span>' if present
               else '<span class="tag gray">未到位</span>'}</td>
          <td>{photo_img}{f'<div class="muted">{ESC(c["checked_by"])} {ESC(c["checked_at"])}</div>' if c else ""}</td>
          <td>
            <label><input type="checkbox" name="present_{p["id"]}" value="1" {"checked" if present else ""} {"disabled" if not can_check else ""}> 已到位</label><br>
            <button form="prot" name="snap" value="{p["id"]}" class="btn sec" {"disabled" if not can_check else ""} style="margin-top:4px">拍照占位</button>
          </td>
        </tr>""")
    pend_add = conn.execute(
        "SELECT COUNT(*) c FROM material_addition WHERE appointment_id=? AND rechecked=0",
        (appt_id,)).fetchone()["c"]

    # 加材料表单
    add_form = ""
    if can_check:
        add_form = f"""
        <div class="card"><h3>临时增加材料（须物业重新核验保护方案）</h3>
        <form method="post" action="/appt/add-material">
          <input type="hidden" name="id" value="{appt_id}">
          <input name="name" placeholder="材料名称" required>
          <input name="weight_kg" type="number" step="0.1" min="0" placeholder="重量kg" required style="width:110px">
          <input name="length_cm" type="number" step="0.1" placeholder="长cm" required style="width:90px">
          <input name="width_cm" type="number" step="0.1" placeholder="宽cm" required style="width:90px">
          <input name="height_cm" type="number" step="0.1" placeholder="高cm" required style="width:90px">
          <label><input type="checkbox" name="overlong" value="1"> 超长</label>
          <button class="btn sec">增加并标记待复核</button>
        </form></div>"""

    # 门岗记录
    grecs = conn.execute("SELECT * FROM gate_record WHERE appointment_id=? ORDER BY id",
                         (appt_id,)).fetchall()
    grow = "".join(
        f'<tr><td>{ESC(r["created_at"])}</td>'
        f'<td><span class="tag {"orange" if r["result"]=="reject" else ""}" '
        f'style="{"background:#c62828;color:#fff" if r["result"]=="reject" else "background:var(--forest);color:#fff"}">'
        f'{ESC(ISSUE_CN.get(r["issue_code"], r["issue_code"] or ""))}</span></td>'
        f'<td>{ESC(r["detail"] or "")}</td></tr>' for r in grecs)

    # 签收表单
    sign_form = ""
    if appt["status"] in ("gate_passed", "arrived"):
        rows_in = "".join(
            f'<tr><td>{ESC(m["name"])}</td><td>{m["weight_kg"]:g}</td>'
            f'<td><input type="number" name="qty_{m["id"]}" value="1" min="0" style="width:70px"></td>'
            f'<td><input type="number" step="0.1" name="w_{m["id"]}" value="{m["weight_kg"]:g}" style="width:110px"></td></tr>'
            for m in mats)
        sign_form = f"""
        <div class="card"><h3>住户签收</h3>
        <form method="post" action="/appt/signoff"><input type="hidden" name="id" value="{appt_id}">
        <p><label>签收人</label> <input name="signed_by" value="{ESC(u["resident"])}" required></p>
        <table><tr><th>材料</th><th>申报kg</th><th>实收数量</th><th>实收重量kg</th></tr>{rows_in}</table>
        <p style="margin-top:10px"><button class="btn">确认签收（终态，不可回退）</button></p></form></div>"""
    signed = conn.execute("SELECT * FROM signoff WHERE appointment_id=?", (appt_id,)).fetchone()
    if signed:
        sitems = conn.execute(
            "SELECT si.*, m.name mn FROM signoff_item si JOIN material m ON m.id=si.material_id "
            "WHERE si.signoff_id=?", (signed["id"],)).fetchall()
        sign_form += '<div class="card"><h3>签收记录</h3><table><tr><th>材料</th><th>申报</th><th>实收</th><th>差异</th></tr>'
        sign_form += "".join(
            f'<tr><td>{ESC(r["mn"])}</td><td>{r["declared_weight_kg"]:g}kg</td>'
            f'<td>{r["received_qty"] if r["received_qty"] is not None else "—"} 件 / '
            f'{r["received_weight_kg"] if r["received_weight_kg"] is not None else "—"}kg</td>'
            f'<td><span class="tag {"orange" if r["diff_code"]!="ok" else "gray"}">'
            f'{ESC({"short":"短少","extra":"超重/多出","ok":"一致","damaged":"损坏"}.get(r["diff_code"], r["diff_code"]))}'
            f'</span></td></tr>' for r in sitems)
        sign_form += f'</table><p class="muted">签收人 {ESC(signed["signed_by"])} · {ESC(signed["signed_at"])}</p></div>'

    body = f"""
    <h2>预约 #{appt_id} {badge(appt["status"])}</h2>
    <div class="card">
      <p><b>{ESC(b["name"])} {ESC(u["room_no"])}</b>　<span class="muted">{ESC(b["address"])}</span><br>
      住户：{ESC(u["resident"])}（门岗端不可见电话）｜承运：{ESC(appt["carrier"])}｜司机：{ESC(appt["driver_name"])}<br>
      车牌：<b style="font-size:20px">{ESC(appt["vehicle_plate"])}</b>
      电梯：{ESC(e["code"])}（额定 {e["load_kg"]}kg）
      时段：{ESC(appt["slot_date"])} <b>{ESC(appt["slot_start"])}–{ESC(appt["slot_end"])}</b></p>
      <p>材料总重：<span class="weight {"over" if over else ""}">{appt["total_weight_kg"]:g} kg</span>
         {"<span class='tag orange'>超重</span>" if over else ""}
         {"<span class='tag orange'>含超长材料</span>" if appt["has_overlong"] else ""}
         {f'<span class="tag orange">{pend_add} 项新增待复核</span>' if pend_add else ""}</p>
    </div>

    <div class="card"><h3>保护点位确认</h3>
      {f'<div class="okbox">物业已于 {ESC(pc["approved_at"])} 由 {ESC(pc["approved_by"])} 确认保护方案，门岗可放行。</div>' if pc and pc["approved"] else ""}
      <form method="post" action="/appt/protection" id="prot">
        <input type="hidden" name="id" value="{appt_id}">
        <table><tr><th>保护点位</th><th>状态</th><th>照片</th><th>演示操作</th></tr>{"".join(prow)}</table>
        <p style="margin-top:10px">
          <button class="btn sec" name="action" value="check" {"disabled" if not can_check else ""}>保存巡检（勾选到位）</button>
          <button class="btn sec" name="snap_all" value="1" {"disabled" if not can_check else ""}>一键拍照占位</button>
          <button class="btn" name="action" value="approve" {"disabled" if not can_check else ""}>物业确认保护方案 → 放行许可</button>
          <label class="muted">确认人 <input name="approved_by" value="物业·王敏" style="width:120px"></label>
        </p>
        <p class="muted">必设点位任一缺失即拒绝确认；临时新增材料未复核时门岗同样拒绝。</p>
      </form>
      <form method="post" action="/appt/elevator-fault" style="margin-top:6px">
        <input type="hidden" name="id" value="{appt_id}">
        <button class="btn sec">演练：{'恢复' if e['status']=='fault' else '模拟'}电梯故障</button>
      </form>
    </div>

    {add_form}

    <div class="card"><h3>流程操作与门岗记录</h3>
      <a class="btn sec" href="/gate/lookup?plate={urllib.parse.quote(appt["vehicle_plate"])}&building={appt["building_id"]}">前往门岗核验</a>
      {'<form method="post" action="/appt/arrive" style="display:inline"><input type="hidden" name="id" value="'+str(appt_id)+'"><button class="btn sec">登记车辆到场</button></form>' if appt["status"]=="gate_passed" else ""}
      <table style="margin-top:12px"><tr><th>时间</th><th>结果</th><th>说明</th></tr>{grow or '<tr><td colspan=3 class=muted>暂无门岗记录</td></tr>'}</table>
    </div>
    {sign_form}"""
    return page(f"预约{appt_id}", "/", body, params.get("msg", [None])[0])


def page_protection_list(conn, params):
    services.expire_holds(conn)
    appts = [dict(r) for r in conn.execute(
        """SELECT a.*, b.name bn, u.room_no FROM appointment a
           JOIN building b ON b.id=a.building_id JOIN unit u ON u.id=a.unit_id
           WHERE a.status IN ('held','confirmed') ORDER BY a.slot_date, a.slot_start""")]
    rows = "".join(
        f'<tr><td>{a["slot_date"]} {a["slot_start"]}</td><td>{ESC(a["bn"])}</td>'
        f'<td>{ESC(a["room_no"])}</td><td>{ESC(a["vehicle_plate"])}</td>'
        f'<td>{badge(a["status"])}</td><td><a class="btn sec" href="/appt?id={a["id"]}">去确认</a></td></tr>'
        for a in appts)
    pts = conn.execute(
        "SELECT b.name bn, p.name, p.kind, p.required FROM protection_point p "
        "JOIN building b ON b.id=p.building_id ORDER BY b.id, p.id").fetchall()
    ptrs = "".join(
        f'<tr><td>{ESC(r["bn"])}</td><td>{ESC(r["name"])}</td>'
        f'<td>{ESC({"floor":"地面","wall":"墙面","corner":"阳角"}[r["kind"]])}</td>'
        f'<td>{"必设" if r["required"] else "选设"}</td></tr>' for r in pts)
    body = f"""
    <h2>保护点位与待确认预约</h2>
    <div class="card"><h3>待物业确认</h3>
      <table><tr><th>时段</th><th>楼栋</th><th>单元</th><th>车牌</th><th>状态</th><th></th></tr>
      {rows or '<tr><td colspan=6 class=muted>暂无待确认预约</td></tr>'}</table></div>
    <div class="card"><h3>各楼栋保护点台账</h3>
      <table><tr><th>楼栋</th><th>点位</th><th>类型</th><th>要求</th></tr>{ptrs}</table></div>"""
    return page("保护点位", "/protection", body, params.get("msg", [None])[0])


def page_gate(conn, params):
    msg = params.get("msg", [None])[0]
    buildings = conn.execute("SELECT id, name FROM building ORDER BY id").fetchall()
    bopts = "".join(f'<option value="{r["id"]}">{ESC(r["name"])}门岗</option>' for r in buildings)
    wb = params.get("wb", ["jitter"])[0]
    body = f"""
    <h2>门岗核验</h2>
    <div class="gate-wrap card">
      <form method="get" action="/gate/lookup">
        <p><label>门岗位置</label><br><select name="building" style="width:100%;font-size:20px">{bopts}</select></p>
        <p><label>车牌识别 / 手动输入</label><br>
          <input class="gate-input" name="plate" value="{ESC(params.get("plate",[""])[0])}" autofocus></p>
        <p><label>地磅适配器</label>
          <select name="wb">
            <option value="jitter" {"selected" if wb=="jitter" else ""}>正常（±3% 波动）</option>
            <option value="exact" {"selected" if wb=="exact" else ""}>精确称重</option>
            <option value="overweight" {"selected" if wb=="overweight" else ""}>演练：超重 120kg</option>
          </select></p>
        <p style="text-align:center"><button class="btn big">核验车辆</button></p>
      </form>
      <p class="muted">门岗端只显示车牌、时段、楼栋单元、电梯与放行结果；住户与司机电话不展示。</p>
    </div>"""
    return page("门岗核验", "/gate", body, msg)


def page_gate_lookup(conn, params):
    plate = params.get("plate", [""])[0].strip()
    bid = int(params.get("building", [0])[0] or 0)
    wb_name = params.get("wb", ["jitter"])[0]
    services.expire_holds(conn)
    row = conn.execute(
        "SELECT * FROM appointment WHERE vehicle_plate=? AND status='confirmed' ORDER BY id DESC LIMIT 1",
        (plate,)).fetchone()
    if not row:
        # 也展示已放行车辆，便于重复扫码演示幂等
        row = conn.execute(
            "SELECT * FROM appointment WHERE vehicle_plate=? "
            "AND status IN ('gate_passed','arrived','signed') ORDER BY id DESC LIMIT 1",
            (plate,)).fetchone()
    if not row:
        return page_gate(conn, {"msg": [f"车牌 {plate} 无待放行预约（可能尚未通过物业确认或已过期释放）"]})

    appt = dict(row)
    view = services.gate_view(conn, appt)
    latest = conn.execute(
        "SELECT * FROM gate_record WHERE appointment_id=? ORDER BY id DESC LIMIT 1",
        (appt["id"],)).fetchone()
    result_box = ""
    if latest:
        ok = latest["result"] == "pass"
        result_box = f"""
        <div class="gate-result {'pass' if ok else 'reject'}">
          {"✅ 放 行" if ok else "⛔ 拒 绝 · " + ESC(ISSUE_CN.get(latest["issue_code"], latest["issue_code"]))}
          <div style="font-size:18px;margin-top:8px">{ESC(latest["detail"] or "")}</div>
        </div>"""
    body = f"""
    <h2>门岗核验结果</h2>
    <div class="gate-wrap">
      <div class="card">
        <div class="gate-plate">{ESC(view["vehicle_plate"])}</div>
        <div class="gate-slot">{ESC(view["slot_date"])}　{ESC(view["slot_start"])}–{ESC(view["slot_end"])}</div>
        {result_box or '<p class="muted" style="text-align:center;font-size:18px">尚未放行，确认无误后按下闸机按钮</p>'}
        <table class="gate-rows">
          <tr><td>前往</td><td><b>{ESC(view["building_name"])} {ESC(view["room_no"])}</b></td></tr>
          <tr><td>电梯</td><td>{ESC(view["elevator_code"])}（额定 {view["load_kg"]:g}kg）</td></tr>
          <tr><td>申报材料</td><td><b>{view["total_weight_kg"]:g} kg</b></td></tr>
          <tr><td>状态</td><td>{badge(view["status"])}</td></tr>
        </table>
        <form method="post" action="/gate/pass" style="text-align:center;margin-top:16px">
          <input type="hidden" name="id" value="{appt["id"]}">
          <input type="hidden" name="building" value="{bid}">
          <input type="hidden" name="wb" value="{ESC(wb_name)}">
          <button class="btn big" {"disabled" if appt["status"] in ("arrived","signed") else ""}>闸机放行</button>
          <a class="btn sec big" href="/appt?id={appt["id"]}" style="font-size:16px">详情</a>
        </form>
        <p class="muted" style="text-align:center">同一时段并发双击仅一辆车放行成功；已放行车辆重复扫码会被拒绝并登记。</p>
      </div>
      <p><a href="/gate">← 返回车牌输入</a></p>
    </div>"""
    return page("门岗核验", "/gate", body)


def page_signoffs(conn, params):
    diffs = services.list_signoff_diffs(conn)
    rows = "".join(
        f'<tr><td>{ESC(r["signed_at"])}</td><td>{ESC(r["building_name"])}</td>'
        f'<td>{ESC(r["room_no"])}</td><td>{ESC(r["vehicle_plate"])}</td>'
        f'<td>{ESC(r["material_name"])}</td>'
        f'<td>{r["declared_weight_kg"]:g}kg</td>'
        f'<td>{r["received_weight_kg"] if r["received_weight_kg"] is not None else "—"}kg / '
        f'{r["received_qty"] if r["received_qty"] is not None else "—"}件</td>'
        f'<td><span class="tag orange">{ESC({"short":"短少","extra":"多出/超重","damaged":"损坏"}.get(r["diff_code"], r["diff_code"]))}</span></td>'
        f'<td>{ESC(r["signed_by"])}</td></tr>' for r in diffs)
    body = f"""
    <h2>签收差异登记</h2>
    <div class="card"><table><tr>
      <th>签收时间</th><th>楼栋</th><th>单元</th><th>车牌</th><th>材料</th>
      <th>申报</th><th>实收</th><th>差异</th><th>签收人</th></tr>
      {rows or '<tr><td colspan=9 class=muted>暂无差异记录（在预约详情页完成签收即可生成）</td></tr>'}
    </table></div>"""
    return page("签收差异", "/signoffs", body, params.get("msg", [None])[0])


# ---------------------------------------------------------------- Handler

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默；demo 脚本另打印启动信息

    def _conn(self):
        c = connect()
        init_db(c)
        return c

    def _send(self, html_text, code=200):
        data = html_text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path, msg=None):
        if msg:
            sep = "&" if "?" in path else "?"
            path = f"{path}{sep}msg={urllib.parse.quote(msg)}"
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        conn = self._conn()
        try:
            route = parsed.path
            if route == "/":
                self._send(page_calendar(conn, params))
            elif route == "/booking":
                self._send(page_booking(conn, params))
            elif route == "/materials":
                self._send(page_materials(conn, params))
            elif route == "/protection":
                self._send(page_protection_list(conn, params))
            elif route == "/gate":
                self._send(page_gate(conn, params))
            elif route == "/gate/lookup":
                self._send(page_gate_lookup(conn, params))
            elif route == "/signoffs":
                self._send(page_signoffs(conn, params))
            elif route == "/appt":
                self._send(page_appt(conn, int(params["id"][0]), params))
            elif route.startswith("/photos/"):
                self._serve_photo(parsed.path)
            elif route == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self._send(page("404", "/", "<h2>404</h2>"), 404)
        except services.BusinessError as ex:
            self._send(page("操作被拒绝", parsed.path,
                            f'<div class="flash">{ESC(str(ex))}</div><p><a href="/">返回日历</a></p>'))
        finally:
            conn.close()

    def _serve_photo(self, path):
        # 仅允许 PHOTO_DIR 内文件，防目录穿越
        name = path.rsplit("/", 1)[-1]
        target = (config.PHOTO_DIR / name).resolve()
        if not str(target).startswith(str(config.PHOTO_DIR.resolve())) or not target.is_file():
            self.send_response(404)
            self.end_headers()
            return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        f = lambda k, d="": form.get(k, [d])[0]
        conn = self._conn()
        try:
            route = urllib.parse.urlparse(self.path).path
            if route == "/book":
                appt = services.create_appointment(
                    conn, unit_id=int(f("unit_id")), elevator_id=int(f("elevator_id")),
                    slot_date=f("date"), slot_start=f("slot_start"),
                    plate=f("plate"), vehicle_adapter=vehicles, note=f("note"))
                self._redirect(f"/appt?id={appt['id']}",
                               "预约占位已创建，请立即完成保护点位确认。")
            elif route == "/appt/add-material":
                aid = int(f("id"))
                services.add_material(
                    conn, aid, name=f("name"), weight_kg=float(f("weight_kg")),
                    length_cm=float(f("length_cm")), width_cm=float(f("width_cm")),
                    height_cm=float(f("height_cm")), overlong=bool(f("overlong")))
                self._redirect(f"/appt?id={aid}", "临时材料已登记，保护方案需重新核验后方可放行。")
            elif route == "/appt/protection":
                aid = int(f("id"))
                points = conn.execute(
                    "SELECT id, name FROM protection_point WHERE building_id="
                    "(SELECT building_id FROM appointment WHERE id=?)", (aid,)).fetchall()
                checks = []
                for p in points:
                    present = bool(f(f"present_{p['id']}"))
                    photo = None
                    snap_target = f("snap")
                    snap_all = bool(f("snap_all"))
                    if snap_all or snap_target == str(p["id"]):
                        photo = photos.capture_demo_photo(aid, p["id"], p["name"])
                        present = True
                    checks.append((p["id"], 1 if (present or photo) else 0, photo))
                services.record_protection_checks(conn, aid, checks, f("approved_by"))
                if f("action") == "approve":
                    try:
                        services.confirm_protection(conn, aid, f("approved_by"))
                        self._redirect(f"/appt?id={aid}", "保护方案已确认，门岗可放行。")
                    except services.BusinessError as ex:
                        self._redirect(f"/appt?id={aid}", f"无法确认：{ex}")
                else:
                    self._redirect(f"/appt?id={aid}", "巡检结果已保存。")
            elif route == "/appt/elevator-fault":
                aid = int(f("id"))
                cur = conn.execute("SELECT status FROM elevator WHERE id="
                                   "(SELECT elevator_id FROM appointment WHERE id=?)", (aid,)).fetchone()
                new = "normal" if cur["status"] == "fault" else "fault"
                conn.execute("UPDATE elevator SET status=? WHERE id="
                             "(SELECT elevator_id FROM appointment WHERE id=?)", (new, aid))
                self._redirect(f"/appt?id={aid}",
                               f"电梯已{'恢复正常' if new == 'normal' else '置为故障，门岗将拒绝放行'}。")
            elif route == "/gate/pass":
                aid, bid, wb = int(f("id")), int(f("building")), f("wb", "jitter")
                view, rec = services.gate_check(
                    conn, appointment_id=aid, gate_building_id=bid,
                    weighbridge=weighbridges.get(wb, weighbridges["jitter"]),
                    vehicle_adapter=vehicles)
                if rec["result"] == "pass":
                    self._redirect(f"/gate/lookup?plate={urllib.parse.quote(view['vehicle_plate'])}"
                                   f"&building={bid}&wb={wb}", "放行成功，闸机已开启。")
                else:
                    self._redirect(f"/gate/lookup?plate={urllib.parse.quote(view['vehicle_plate'])}"
                                   f"&building={bid}&wb={wb}",
                                   f"已登记拒绝：{ISSUE_CN.get(rec['issue_code'], rec['issue_code'])}")
            elif route == "/appt/arrive":
                aid = int(f("id"))
                services.mark_arrived(conn, aid)
                self._redirect(f"/appt?id={aid}", "车辆到场已登记，可进行签收。")
            elif route == "/appt/signoff":
                aid = int(f("id"))
                _, _, mats = services._unit_material_totals(
                    conn, services.get_appointment(conn, aid)["unit_id"], aid)
                items = []
                for m in mats:
                    q = f(f"qty_{m['id']}")
                    w = f(f"w_{m['id']}")
                    items.append((m["id"], int(q) if q else None,
                                  float(w) if w else None))
                payload = services.signoff(conn, aid, f("signed_by"), items, f("note"))
                self._redirect(f"/appt?id={aid}",
                               "重复提交已幂等忽略，签收记录唯一。" if payload["idempotent"]
                               else "签收完成（终态，不可回退为未到场）。")
            else:
                self._send(page("404", "/", "<h2>404</h2>"), 404)
        except services.BusinessError as ex:
            back = self.headers.get("Referer", "/")
            self._redirect(back if urllib.parse.urlparse(back).path else "/", str(ex))
        except (ValueError, KeyError) as ex:
            self._redirect("/", f"表单参数错误：{ESC(str(ex))}")
        finally:
            conn.close()


def serve(port=None):
    port = port or config.HTTP_PORT
    conn = connect()
    init_db(conn)
    seed(conn, verbose=False)
    conn.close()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"服务已启动：http://localhost:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
