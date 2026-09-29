"""Web 界面：标准库 http.server，零依赖。

页面：楼栋日历 / 材料清单 / 保护点位 / 车辆预约 / 门岗核验 / 签收差异 / 异常登记。
每次请求先执行过期预约释放（占位过期自动释放占用）。
"""
import html
import os
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import adapters, config, services
from .db import connect, init_db
from .seed import seed

STATUS_LABELS = {
    config.ST_HELD: "待确认保护",
    config.ST_PROTECTED: "已确认可放行",
    config.ST_PASSED: "已放行",
    config.ST_SIGNED: "已签收",
    config.ST_EXPIRED: "已过期",
    config.ST_CANCELLED: "已取消",
}
STATUS_COLORS = {
    config.ST_HELD: "#b08900",
    config.ST_PROTECTED: config.COLOR_FOREST_2,
    config.ST_PASSED: config.COLOR_ORANGE_DARK,
    config.ST_SIGNED: config.COLOR_FOREST,
    config.ST_EXPIRED: "#777",
    config.ST_CANCELLED: "#777",
}

CSS = """
:root{--forest:#1b4332;--forest2:#2d6a4f;--orange:#f77f00;--orange-d:#e85d04;}
*{box-sizing:border-box;}
body{margin:0;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
 background:#fff;color:#1a1a1a;}
header{background:var(--forest);color:#fff;padding:14px 28px;display:flex;
 align-items:baseline;gap:24px;flex-wrap:wrap;}
header h1{font-size:20px;margin:0;}
header nav a{color:#d8f3dc;text-decoration:none;margin-right:16px;font-size:15px;}
header nav a:hover{color:var(--orange);}
main{max-width:1180px;margin:0 auto;padding:24px;}
h2{color:var(--forest);border-left:5px solid var(--orange);padding-left:10px;}
.card{border:1px solid #d8e2dc;border-radius:8px;padding:16px;margin-bottom:18px;background:#fff;}
table{border-collapse:collapse;width:100%;background:#fff;}
th,td{border:1px solid #e0e0e0;padding:8px 10px;text-align:left;font-size:14px;}
th{background:var(--forest);color:#fff;font-weight:600;}
tr:nth-child(even) td{background:#f6f9f7;}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;font-size:12px;}
.weight{font-size:22px;font-weight:800;color:var(--orange-d);font-variant-numeric:tabular-nums;}
.weight.over{color:#c1121f;}
.tag-over{background:var(--orange);color:#fff;padding:1px 8px;border-radius:4px;
 font-size:12px;font-weight:700;margin-left:6px;}
.tag-ok{background:var(--forest2);color:#fff;padding:1px 8px;border-radius:4px;font-size:12px;}
.tag-recheck{background:var(--orange-d);color:#fff;padding:1px 8px;border-radius:4px;font-size:12px;}
input,select{padding:7px 9px;border:1px solid #bbb;border-radius:5px;font-size:14px;margin:3px 0;}
button,.btn{background:var(--orange);color:#fff;border:none;padding:9px 20px;border-radius:6px;
 font-size:15px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block;}
button:hover,.btn:hover{background:var(--orange-d);}
.btn-forest{background:var(--forest2);}
.btn-forest:hover{background:var(--forest);}
.flash-ok{background:#d8f3dc;border-left:5px solid var(--forest2);padding:10px 14px;margin:10px 0;}
.flash-err{background:#ffe5e5;border-left:5px solid #c1121f;padding:10px 14px;margin:10px 0;}
/* 日历 */
.cal-grid{display:grid;gap:10px;}
.cal-col{border:1px solid #cdd8d2;border-radius:8px;overflow:hidden;}
.cal-col h3{margin:0;background:var(--forest2);color:#fff;padding:8px;text-align:center;font-size:15px;}
.slot-cell{padding:8px;border-top:1px solid #e0e0e0;min-height:64px;font-size:13px;}
.slot-cell .t{font-weight:700;color:var(--forest);}
.appt-chip{margin-top:5px;padding:6px 8px;border-radius:6px;background:#f6f9f7;border:1px solid #d8e2dc;}
/* 门岗大字号 */
.gate-box{border:3px solid var(--forest);border-radius:12px;padding:28px;text-align:center;}
.gate-plate{font-size:64px;font-weight:900;letter-spacing:3px;color:var(--forest);
 font-variant-numeric:tabular-nums;margin:6px 0;}
.gate-slot{font-size:34px;font-weight:800;color:#222;margin:8px 0;}
.gate-pass{font-size:42px;font-weight:900;padding:18px;border-radius:10px;margin-top:14px;}
.gate-pass.ok{background:#d8f3dc;color:var(--forest);}
.gate-pass.no{background:#ffe5e5;color:#c1121f;}
.gate-meta{font-size:20px;color:#444;}
.photo{max-width:180px;border:2px solid var(--forest);border-radius:6px;}
.muted{color:#777;font-size:13px;}
.diff-row td.mismatch{background:#fff3e0 !important;font-weight:700;}
"""


def esc(v):
    return html.escape("" if v is None else str(v))


def status_badge(status):
    return (f'<span class="badge" style="background:{STATUS_COLORS.get(status, "#777")}">'
            f'{STATUS_LABELS.get(status, status)}</span>')


class App:
    """每线程一个 SQLite 连接（ThreadingHTTPServer 每请求一个线程）。"""

    def __init__(self, db_path=None):
        self._db_path = db_path
        self._local = threading.local()

    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = connect(self._db_path)
            self._local.conn = c
        return c

    # --------------------------------------------------------- 辅助查询
    def buildings(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM building ORDER BY id")]

    def elevators(self, bid):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM elevator WHERE building_id=? ORDER BY id", (bid,))]

    def docks(self, bid):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM loading_dock WHERE building_id=? ORDER BY id", (bid,))]

    def units(self, bid):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM unit WHERE building_id=? ORDER BY room_no", (bid,))]

    def page(self, title, body):
        nav = [("/", "楼栋日历"), ("/materials", "材料清单"), ("/protection", "保护点位"),
               ("/booking", "车辆预约"), ("/gate", "门岗核验"), ("/signoff", "签收差异"),
               ("/exceptions", "异常登记")]
        links = "".join(f'<a href="{u}">{n}</a>' for u, n in nav)
        return f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>{esc(title)}</title><style>{CSS}</style></head><body>
<header><h1>装修材料进场 · 电梯保护确认系统</h1><nav>{links}</nav></header>
<main><h2>{esc(title)}</h2>{body}</main></body></html>"""

    # --------------------------------------------------------- 楼栋日历
    def calendar(self, q):
        bids = self.buildings()
        bid = int(q.get("b", [bids[0]["id"]])[0]) if bids else 0
        date = q.get("d", [datetime.now().strftime("%Y-%m-%d")])[0]
        services.release_expired(self.conn, datetime.now())
        data = services.calendar(self.conn, bid, date)
        opts = "".join(f'<option value="{b["id"]}" {"selected" if b["id"]==bid else ""}>'
                       f'{esc(b["name"])}</option>' for b in bids)
        cols = []
        for e in data["elevators"]:
            fault = ' <span class="tag-recheck">故障</span>' if e["status"] == "fault" else ""
            cells = []
            for s, se in services.SLOTS:
                chips = []
                for a in data["grid"].get(e["id"], {}).get(s, []):
                    chips.append(
                        f'<div class="appt-chip">{esc(a["plate"])} · {esc(a["room_no"])} '
                        f'{status_badge(a["status"])}<br><span class="muted">{esc(a["code"])}'
                        f' · {a["list_weight"]:.0f}kg{" · 超长" if a["list_overlong"] else ""}'
                        f'</span></div>')
                cells.append(f'<div class="slot-cell"><span class="t">{s}-{se}</span>'
                             f'{"".join(chips) or "<span class=muted>空闲</span>"}</div>')
            cols.append(f'<div class="cal-col"><h3>{esc(e["code"])}'
                        f'<span class=muted>（限重 {e["max_load_kg"]:.0f}kg）</span>{fault}'
                        f'</h3>{"".join(cells)}</div>')
        grid = (f'<div class="cal-grid" style="grid-template-columns:repeat({len(cols) or 1},1fr)">'
                f'{"".join(cols)}</div>')
        body = f"""
        <form class=card method=get>
          楼栋 <select name=b>{opts}</select>
          日期 <input type=date name=d value="{esc(date)}">
          <button class="btn-forest">查看日历</button>
        </form>
        <div class=card>{grid}</div>"""
        return self.page("楼栋日历（按电梯分栏）", body)

    # --------------------------------------------------------- 材料清单
    def materials(self, q, flash=None):
        bids = self.buildings()
        bid = int(q.get("b", [bids[0]["id"]])[0]) if bids else 0
        units = self.units(bid)
        uid = int(q.get("u", [units[0]["id"] if units else 0])[0])
        body = flash or ""
        bopts = "".join(f'<option value="{b["id"]}" {"selected" if b["id"]==bid else ""}>'
                        f'{esc(b["name"])}</option>' for b in bids)
        uopts = "".join(f'<option value="{u["id"]}" {"selected" if u["id"]==uid else ""}>'
                        f'{esc(u["room_no"])} {esc(u["resident"])}</option>' for u in units)
        body += f"""
        <form class=card method=get>
          楼栋 <select name=b onchange="this.form.submit()">{bopts}</select>
          装修单元 <select name=u onchange="this.form.submit()">{uopts}</select>
          <span class=muted>住户电话仅在演示数据中，门岗端不显示</span>
        </form>"""
        if uid:
            lid = services.unit_active_list(self.conn, uid)
            if lid:
                body += self._material_detail(lid)
        return self.page("材料清单", body)

    def _material_detail(self, lid, form=None):
        form = form or {}
        d = services.list_detail(self.conn, lid)
        lst, items = d["list"], d["items"]
        elevs = self.conn.execute("""SELECT e.* FROM elevator e JOIN unit u ON u.building_id=e.building_id
            WHERE u.id=?""", (lst["unit_id"],)).fetchall()
        max_load = max((e["max_load_kg"] for e in elevs), default=1000)
        over_weight = lst["total_weight"] > max_load
        rows = []
        for i in items:
            dims = f'{i["length_m"]}×{i["width_m"]}×{i["height_m"]}m'
            rows.append(
                f'<tr><td>{esc(i["name"])}</td><td>{i["qty"]:g}{esc(i["unit"])}</td>'
                f'<td class="weight {"over" if i["weight_kg"]>max_load else ""}">{i["weight_kg"]:.0f}</td>'
                f'<td>{dims} {"<span class=tag-over>超长</span>" if i["overlong"] else ""}</td></tr>')
        tag = ('<span class="tag-ok">已核验</span>' if lst["status"] == "verified"
               else '<span class="tag-recheck">待重新核验</span>')
        recheck_btn = ""
        if lst["status"] == "recheck":
            recheck_btn = f'''
            <form method=post action="/materials/verify?lid={lid}" style="margin-top:8px">
              <button class="btn-forest">物业重新核验清单 v{lst["version"]}</button></form>'''
        add_form = f"""
        <div class=card>
          <h3 style="margin-top:0">临时增加材料（清单版本 +1，须重新核验）</h3>
          <form method=post action="/materials/add?lid={lid}">
            名称 <input name=name required placeholder="如 木工板">
            数量 <input name=qty type=number step=0.01 value=1 style="width:80px">
            单位 <input name=unit value="件" style="width:70px">
            重量kg <input name=weight_kg type=number step=0.1 required style="width:90px">
            长m <input name=length_m type=number step=0.01 value=0 style="width:80px">
            宽m <input name=width_m type=number step=0.01 value=0 style="width:80px">
            高m <input name=height_m type=number step=0.01 value=0 style="width:80px">
            <button>增加</button>
          </form>
        </div>"""
        return f"""
        <div class=card>
          清单 v{lst["version"]} {tag} · 更新 {esc(lst["updated_at"])}
          <div style="margin:10px 0">合计重量
            <span class="weight {'over' if over_weight else ''}">{lst["total_weight"]:.1f} kg</span>
            {'<span class="tag-over">超过电梯载重</span>' if over_weight else ""}
            {'<span class="tag-over">含超长材料</span>' if lst["has_overlong"] else ""}
          </div>
          <table><tr><th>材料</th><th>数量</th><th>重量(kg)</th><th>尺寸</th></tr>
          {"".join(rows)}</table>
          {recheck_btn}
        </div>{add_form}"""

    def material_add(self, lid, form, now):
        services.add_material_item(self.conn, lid, form, now)
        return f'<div class="flash-ok">已增加材料，清单版本 +1，状态置为「待重新核验」。</div>'

    def material_verify(self, lid, now):
        services.verify_material_list(self.conn, lid, now)
        return '<div class="flash-ok">材料清单已重新核验。</div>'

    # --------------------------------------------------------- 车辆预约
    def booking(self, q, form=None, flash=None):
        bids = self.buildings()
        bid = int((form or q).get("building_id", [bids[0]["id"]])[0]) if bids else 0
        elevs, docks, units = self.elevators(bid), self.docks(bid), self.units(bid)
        date = q.get("d", [datetime.now().strftime("%Y-%m-%d")])[0]
        body = flash or ""
        bopts = "".join(f'<option value="{b["id"]}" {"selected" if b["id"]==bid else ""}>'
                        f'{esc(b["name"])}</option>' for b in bids)
        eopts = "".join(f'<option value="{e["id"]}">{esc(e["code"])}'
                        f'（限重 {e["max_load_kg"]:.0f}kg'
                        f'{" 故障" if e["status"]=="fault" else ""}）</option>' for e in elevs)
        dopts = "".join(f'<option value="{d["id"]}">{esc(d["code"])}</option>' for d in docks)
        uopts = "".join(f'<option value="{u["id"]}">{esc(u["room_no"])} {esc(u["resident"])}'
                        f'</option>' for u in units)
        sopts = "".join(f'<option value="{s}">{s}-{e}</option>' for s, e in services.SLOTS)
        popts = "".join(f'<option value="{esc(p)}">{esc(p)}</option>'
                        for p in adapters.VehicleAdapter().all_plates())
        body += f"""
        <form class=card method=post action="/booking/create">
          <p>楼栋 <select name=building_id>{bopts}</select>
             电梯 <select name=elevator_id>{eopts}</select>
             装卸区 <select name=dock_id>{dopts}</select></p>
          <p>装修单元 <select name=unit_id>{uopts}</select></p>
          <p>车牌 <select name=plate>{popts}</select>
             司机 <input name=driver value="王师傅" required></p>
          <p>进场日期 <input type=date name=slot_date value="{esc(date)}">
             时段 <select name=slot_start>{sopts}</select></p>
          <button>创建预约（占位 {config.HOLD_MINUTES} 分钟）</button>
        </form>"""
        body += self._appt_table(self.conn.execute(
            "SELECT * FROM appointment ORDER BY id DESC LIMIT 10").fetchall())
        return self.page("车辆预约", body)

    def _appt_table(self, rows):
        out = ['<div class=card><h3 style="margin-top:0">最近预约</h3><table>',
               "<tr><th>单号</th><th>楼栋/单元</th><th>电梯</th><th>车牌</th><th>时段</th>"
               "<th>重量</th><th>状态</th><th>操作</th></tr>"]
        for r in rows:
            r = dict(r)
            b = self.conn.execute("SELECT name FROM building WHERE id=?",
                                  (r["building_id"],)).fetchone()["name"]
            u = self.conn.execute("SELECT room_no FROM unit WHERE id=?",
                                  (r["unit_id"],)).fetchone()["room_no"]
            e = self.conn.execute("SELECT code FROM elevator WHERE id=?",
                                  (r["elevator_id"],)).fetchone()["code"]
            link = ""
            if r["status"] == config.ST_HELD:
                link = f'<a class="btn btn-forest" href="/protection?a={r["id"]}">去确认保护</a>'
            elif r["status"] == config.ST_PROTECTED:
                link = f'<a class="btn" href="/gate?a={r["id"]}">门岗核验</a>'
            elif r["status"] == config.ST_PASSED:
                link = f'<a class="btn btn-forest" href="/signoff?a={r["id"]}">签收</a>'
            out.append(
                f'<tr><td>{esc(r["code"])}</td><td>{esc(b)} {esc(u)}</td><td>{esc(e)}</td>'
                f'<td>{esc(r["plate"])}</td><td>{r["slot_date"]} {r["slot_start"]}</td>'
                f'<td class="weight">{r["list_weight"]:.0f}'
                f'{"<span class=tag-over>超长</span>" if r["list_overlong"] else ""}</td>'
                f'<td>{status_badge(r["status"])}</td><td>{link}</td></tr>')
        out.append("</table></div>")
        return "".join(out)

    def booking_create(self, form, now):
        f = {k: v[0] for k, v in form.items()}
        lid = services.unit_active_list(self.conn, int(f["unit_id"]))
        services.create_appointment(
            self.conn, int(f["building_id"]), int(f["elevator_id"]), int(f["dock_id"]),
            int(f["unit_id"]), f["plate"], f["driver"], f["slot_date"], f["slot_start"],
            lid, now)
        return f'<div class="flash-ok">预约已创建并占位，等待物业确认保护方案（{config.HOLD_MINUTES} 分钟内有效）。</div>'

    # --------------------------------------------------------- 保护点位
    def protection(self, q, flash=None):
        rows = self.conn.execute(
            "SELECT * FROM appointment WHERE status IN (?,?) ORDER BY id DESC",
            (config.ST_HELD, config.ST_PROTECTED)).fetchall()
        aopts = "".join(
            f'<option value="{r["id"]}" {"selected" if str(r["id"])==q.get("a",[""])[0] else ""}>'
            f'{r["code"]} · {r["plate"]} · {r["slot_date"]} {r["slot_start"]}'
            f' · {STATUS_LABELS[r["status"]]}</option>' for r in rows)
        body = flash or ""
        body += f"""
        <form class=card method=get>待确认预约
          <select name=a onchange="this.form.submit()"><option value=0>请选择</option>{aopts}</select>
        </form>"""
        aid = q.get("a", ["0"])[0]
        if aid and aid != "0":
            body += self._protection_detail(int(aid))
        return self.page("保护点位确认", body)

    def _protection_detail(self, aid):
        appt = services.get_appointment(self.conn, aid)
        points = services.protection_status(self.conn, aid)
        rows = []
        for p in points:
            photo = (f'<img class=photo src="/photo/{esc(p["photo_path"])}">'
                     if p["photo_path"] else '<span class="muted">未拍照（确认时自动生成占位照片）</span>')
            rows.append(
                f'<tr><td>{esc(p["name"])}{" <span class=tag-over>必检</span>" if p["required"] else ""}</td>'
                f'<td><label><input type=checkbox name="ok_{p["id"]}" checked> 已铺设保护板</label></td>'
                f'<td>{photo}</td></tr>')
        done = appt["status"] != config.ST_HELD
        return f"""
        <div class=card>单号 {esc(appt["code"])} · {esc(appt["plate"])} ·
          {appt["slot_date"]} {appt["slot_start"]}-{appt["slot_end"]} {status_badge(appt["status"])}
        </div>
        <form class=card method=post action="/protection/confirm?a={aid}">
          <table><tr><th>保护点位</th><th>检查结果</th><th>保护照片（本地目录）</th></tr>
          {"".join(rows)}</table>
          <p>确认人 <input name=confirmed_by value="物业张主管" required>
          <button {'disabled' if done else ''}>{"已确认" if done else "确认保护方案并放行待进场"}</button></p>
          <p class=muted>材料清单在预约后被临时增改时，必须先重新核验清单才能确认。</p>
        </form>"""

    def protection_confirm(self, aid, form, now):
        point_ids = [p["id"] for p in services.protection_status(self.conn, aid)]
        checks = {pid: {"ok": f"ok_{pid}" in form} for pid in point_ids}
        services.confirm_protection(
            self.conn, aid, checks, form.get("confirmed_by", ["物业"])[0], now)
        return '<div class="flash-ok">保护方案已确认，门岗可以放行。</div>'

    # --------------------------------------------------------- 门岗核验
    def gate(self, q, flash=None):
        bids = self.buildings()
        bid = int(q.get("b", [bids[0]["id"]])[0]) if bids else 0
        bopts = "".join(f'<option value="{b["id"]}" {"selected" if b["id"]==bid else ""}>'
                        f'{esc(b["name"])}</option>' for b in bids)
        body = flash or ""
        body += f"""
        <form class=card method=post action="/gate/check">
          门岗楼栋 <select name=building_id>{bopts}</select><br><br>
          车牌（大字输入）<input name=plate style="font-size:28px;width:300px;font-weight:800"
            placeholder="沪A·12345" required autofocus>
          <input type=hidden name=guard value="门岗老刘">
          <button style="font-size:22px;padding:12px 30px">核验放行</button>
        </form>
        <p class=muted>门岗端只读取车牌、楼栋、时段、保护状态、重量等放行必要字段；
           不显示住户姓名、电话、房号。</p>"""
        aid = q.get("a", [""])[0]
        if aid:
            body += self._gate_result(int(aid))
        return self.page("门岗核验", body)

    def _gate_result(self, aid):
        v = services.gate_view(self.conn, aid)
        return f"""
        <div class="gate-box">
          <div class="gate-meta">{esc(v["building"])} · {esc(v["elevator"])} · {esc(v["code"])}</div>
          <div class="gate-plate">{esc(v["plate"])}</div>
          <div class="gate-slot">{esc(v["slot"])}</div>
          <div class="gate-meta">申报重量 {v["list_weight"]:.0f}kg
            {"<span class=tag-over>超长</span>" if v["list_overlong"] else ""} · 司机 {esc(v["driver"])}</div>
          <div class="gate-pass {'ok' if v['status']==config.ST_PASSED else 'no'}">
            {"✅ 放行通过" if v["status"]==config.ST_PASSED else "⛔ " + esc(STATUS_LABELS[v["status"]])}
          </div>
        </div>"""

    def gate_check(self, form, now):
        f = {k: v[0] for k, v in form.items()}
        services.release_expired(self.conn, now)
        res = services.gate_pass(self.conn, f["plate"], int(f["building_id"]),
                                 f.get("guard", "门岗"), now)
        if res["passed"]:
            flash = (f'<div class="flash-ok">✅ 放行成功：{esc(f["plate"])} · '
                     f'实称货物 {res.get("weighed_kg", 0):.0f}kg · 单号 {esc(res["view"]["code"])}</div>')
        else:
            flash = f'<div class="flash-err">⛔ 拒绝放行：{esc(res["reason"])}</div>'
        aid = res["appointment_id"]
        return self.gate({"b": [f["building_id"]], **({"a": [str(aid)]} if aid else {})}, flash)

    # --------------------------------------------------------- 签收
    def signoff(self, q, flash=None):
        rows = self.conn.execute(
            "SELECT * FROM appointment WHERE status IN (?,?) ORDER BY id DESC",
            (config.ST_PASSED, config.ST_SIGNED)).fetchall()
        aopts = "".join(
            f'<option value="{r["id"]}" {"selected" if str(r["id"])==q.get("a",[""])[0] else ""}>'
            f'{r["code"]} · {r["plate"]} · {STATUS_LABELS[r["status"]]}</option>' for r in rows)
        body = flash or ""
        body += f"""
        <form class=card method=get>待签收/已签收
          <select name=a onchange="this.form.submit()"><option value=0>请选择</option>{aopts}</select>
        </form>"""
        aid = q.get("a", ["0"])[0]
        if aid and aid != "0":
            body += self._signoff_detail(int(aid))
        return self.page("签收与差异登记", body)

    def _signoff_detail(self, aid):
        appt = services.get_appointment(self.conn, aid)
        done = self.conn.execute(
            "SELECT * FROM signoff WHERE appointment_id=?", (aid,)).fetchone()
        if done:
            items = services._signoff_items(self.conn, done["id"])
            rows = "".join(
                f'<tr class="diff-row {"mismatch" if i["expected_qty"]!=i["actual_qty"] else ""}">'
                f'<td>{esc(i["name"])}</td><td>{i["expected_qty"]:g}{esc(i["unit"])}</td>'
                f'<td>{i["actual_qty"]:g}{esc(i["unit"])}</td></tr>' for i in items)
            head = ('<div class="flash-ok">签收已完成（重复提交返回首次记录，签收幂等）。'
                    '一致 ✅</div>' if done["matched"]
                    else '<div class="flash-err">签收存在差异 ⚠ 已登记签收差异异常</div>')
            return f"""{head}<div class=card>签收人 {esc(done["receiver"])} · {esc(done["created_at"])}
              <table><tr><th>材料</th><th>应收</th><th>实收</th></tr>{rows}</table></div>"""

        lid = services.unit_active_list(self.conn, appt["unit_id"])
        items = services.list_detail(self.conn, lid)["items"]
        rows = "".join(
            f'<tr><td>{esc(i["name"])}</td><td>{i["qty"]:g}{esc(i["unit"])}</td>'
            f'<td><input name="qty_{i["id"]}" type=number step=0.01 value="{i["qty"]:g}" '
            f'style="width:90px"> {esc(i["unit"])}</td></tr>' for i in items)
        return f"""
        <form class=card method=post action="/signoff/submit?a={aid}">
          <table><tr><th>材料</th><th>应收</th><th>实收</th></tr>{rows}</table>
          <p>签收人 <input name=receiver value="住户本人" required>
          <button>确认签收</button></p>
          <p class=muted>签收为终态：已签收材料不能回退为未到场；数量不一致自动登记差异。</p>
        </form>"""

    def signoff_submit(self, aid, form, now):
        appt = services.get_appointment(self.conn, aid)
        lid = services.unit_active_list(self.conn, appt["unit_id"])
        items = services.list_detail(self.conn, lid)["items"]
        actual = [{"name": i["name"], "unit": i["unit"],
                   "qty": float(form.get(f"qty_{i['id']}", [i["qty"]])[0])} for i in items]
        res = services.signoff(self.conn, aid, form.get("receiver", ["签收人"])[0], actual, now)
        if res["idempotent"]:
            flash = '<div class="flash-ok">该预约已签收，返回首次签收记录（幂等）。</div>'
        elif res["signoff"]["matched"]:
            flash = '<div class="flash-ok">签收完成，清单与实收一致。</div>'
        else:
            flash = '<div class="flash-err">签收完成，存在差异，已登记签收差异异常。</div>'
        return flash

    # --------------------------------------------------------- 异常登记
    def exceptions(self):
        rows = services.exceptions(self.conn)
        trs = []
        for r in rows:
            code = ""
            if r["appointment_id"]:
                a = self.conn.execute("SELECT code,plate FROM appointment WHERE id=?",
                                      (r["appointment_id"],)).fetchone()
                if a:
                    code = f'{esc(a["code"])} · {esc(a["plate"])}'
            label = config.EXCEPTION_LABELS.get(r["kind"], r["kind"])
            trs.append(f'<tr><td>{esc(r["created_at"])}</td>'
                       f'<td><span class="tag-over">{esc(label)}</span></td>'
                       f'<td>{code}</td><td>{esc(r["detail"])}</td></tr>')
        body = f"""<div class=card><table>
          <tr><th>时间</th><th>类型</th><th>预约/车牌</th><th>详情</th></tr>
          {"".join(trs) or '<tr><td colspan=4 class=muted>暂无异常</td></tr>'}</table></div>"""
        return self.page("异常登记", body)


# ---------------------------------------------------------------- HTTP 分发

class Handler(BaseHTTPRequestHandler):
    app = None

    def _send(self, content, code=200, ctype="text/html; charset=utf-8"):
        body = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path):
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def _parse_form(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        app = self.app
        try:
            if path == "/":
                self._send(app.calendar(qs))
            elif path == "/materials":
                self._send(app.materials(qs))
            elif path == "/booking":
                self._send(app.booking(qs))
            elif path == "/protection":
                self._send(app.protection(qs))
            elif path == "/gate":
                self._send(app.gate(qs))
            elif path == "/signoff":
                self._send(app.signoff(qs))
            elif path == "/exceptions":
                self._send(app.exceptions())
            elif path.startswith("/photo/"):
                self._photo(path[len("/photo/"):])
            else:
                self._send("Not Found", 404, "text/plain; charset=utf-8")
        except services.DomainError as e:
            self._send(app.page("操作被拒绝", f'<div class="flash-err">{esc(str(e))}</div>'))
        except Exception as e:  # noqa: BLE001
            self._send(app.page("服务器错误", f'<div class="flash-err">{esc(repr(e))}</div>'), 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        form = self._parse_form()
        now = datetime.now()
        app = self.app
        try:
            if path == "/materials/add":
                lid = int(qs["lid"][0])
                app.material_add(lid, {k: v[0] for k, v in form.items()}, now)
                self._redirect(f"/materials?{self._back_to_list(lid)}")
            elif path == "/materials/verify":
                lid = int(qs["lid"][0])
                app.material_verify(lid, now)
                self._redirect(f"/materials?{self._back_to_list(lid)}")
            elif path == "/booking/create":
                app.booking_create(form, now)
                self._redirect("/booking")
            elif path == "/protection/confirm":
                app.protection_confirm(int(qs["a"][0]), form, now)
                self._redirect(f"/protection?a={qs['a'][0]}")
            elif path == "/gate/check":
                self._send(app.gate_check(form, now))
            elif path == "/signoff/submit":
                flash = app.signoff_submit(int(qs["a"][0]), form, now)
                self._send(app.signoff({"a": qs["a"]}, flash))
            else:
                self._send("Not Found", 404, "text/plain; charset=utf-8")
        except services.DomainError as e:
            back = qs.get("from", ["/"])[0]
            self._send(app.page("操作被拒绝", f'<div class="flash-err">{esc(str(e))}</div>'))
        except Exception as e:  # noqa: BLE001
            self._send(app.page("服务器错误", f'<div class="flash-err">{esc(repr(e))}</div>'), 500)

    def _back_to_list(self, lid):
        row = self.app.conn.execute(
            """SELECT u.id uid, u.building_id bid FROM material_list ml
               JOIN unit u ON u.id=ml.unit_id WHERE ml.id=?""", (lid,)).fetchone()
        return f"b={row['bid']}&u={row['uid']}" if row else ""

    def _photo(self, name):
        name = urllib.parse.unquote(name)
        p = adapters.photo_abspath(name)
        if config.PHOTO_DIR.resolve() not in p.resolve().parents and p.resolve() != config.PHOTO_DIR.resolve():
            self._send("forbidden", 403, "text/plain")
            return
        if not p.exists():
            self._send("not found", 404, "text/plain")
            return
        self._send(p.read_bytes(), 200, "image/png")

    def log_message(self, fmt, *args):
        pass


def make_server(host="127.0.0.1", port=8000, db_path=None):
    conn = init_db(db_path)
    seed(conn)
    conn.close()
    Handler.app = App(db_path)
    return ThreadingHTTPServer((host, port), Handler)
