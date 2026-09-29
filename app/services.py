"""领域服务：预约分配、保护确认、门岗放行、签收。

并发约定
--------
* 抢占时段：``elevator+slot`` 部分唯一索引保证一梯一时段一车；
  装卸区用 ``bay_ticket`` 把车位变成唯一行，库层兜底，服务层捕获冲突。
* 门岗确认：在 ``BEGIN IMMEDIATE`` 事务内做条件更新
  （``WHERE status='confirmed'``），并发双击只有一辆车成功。
* 签收：``signoff`` 对预约唯一，重复提交幂等返回首条，不产生第二条。
"""
import sqlite3
import time
import uuid
from datetime import datetime, timedelta

from . import config
from .db import immediate_tx

SLOT_FMT = "%H:%M"
DATE_FMT = "%Y-%m-%d"


class BusinessError(Exception):
    """规则拒绝（含面向门岗的 issue_code）。"""

    def __init__(self, message, issue_code=None):
        super().__init__(message)
        self.issue_code = issue_code


class CapacityError(BusinessError):
    pass


# ---------------------------------------------------------------- 时间工具

def _now() -> float:
    return time.time()


def slot_labels(date_str: str):
    """生成某日的全部 (start, end) 时段标签。"""
    start = datetime.strptime(f"{date_str} {config.DAY_START}", f"{DATE_FMT} {SLOT_FMT}")
    end = datetime.strptime(f"{date_str} {config.DAY_END}", f"{DATE_FMT} {SLOT_FMT}")
    step = timedelta(minutes=config.SLOT_MINUTES)
    out = []
    cur = start
    while cur + step <= end:
        out.append((cur.strftime(SLOT_FMT), (cur + step).strftime(SLOT_FMT)))
        cur += step
    return out


def _slot_epoch(date_str, hhmm, end=False):
    t = datetime.strptime(f"{date_str} {hhmm}", f"{DATE_FMT} {SLOT_FMT}")
    return t.timestamp()


# ---------------------------------------------------------------- 照片

def save_photo(appointment_id: int, point_id: int, data: bytes, suffix: str = "jpg") -> str:
    """保存保护板照片到本地目录，返回库内存储的相对路径。"""
    config.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    name = f"a{appointment_id}_p{point_id}_{uuid.uuid4().hex[:8]}.{suffix}"
    path = config.PHOTO_DIR / name
    path.write_bytes(data)
    return f"photos/{name}"


# ---------------------------------------------------------------- 查询

def get_building_map(conn):
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM building")}


def calendar(conn, date_str):
    """楼栋日历：按楼栋 → 电梯分栏返回时段占用。"""
    buildings = [dict(r) for r in conn.execute("SELECT * FROM building ORDER BY id")]
    result = []
    for b in buildings:
        elevators = [dict(r) for r in conn.execute(
            "SELECT * FROM elevator WHERE building_id=? ORDER BY code", (b["id"],))]
        cols = []
        for e in elevators:
            rows = conn.execute(
                """SELECT a.*, u.room_no FROM appointment a
                   JOIN unit u ON u.id=a.unit_id
                   WHERE a.elevator_id=? AND a.slot_date=?
                     AND a.status IN ('held','confirmed','gate_passed','arrived','signed')
                   ORDER BY a.slot_start""",
                (e["id"], date_str)).fetchall()
            cols.append({"elevator": e, "slots": [dict(r) for r in rows]})
        result.append({"building": b, "elevators": cols})
    return {"date": date_str, "slot_labels": slot_labels(date_str), "buildings": result}


def bay_used(conn, building_id, date_str, start):
    row = conn.execute(
        """SELECT COUNT(*) c FROM appointment
           WHERE building_id=? AND slot_date=? AND slot_start=?
             AND status IN ('held','confirmed','gate_passed','arrived','signed')""",
        (building_id, date_str, start)).fetchone()
    return row["c"]


# ---------------------------------------------------------------- 预约

def _unit_material_totals(conn, unit_id, appointment_id=None):
    """汇总单元材料；临时新增材料也挂在同一 unit_id 下，
    material_addition 仅作为"需重新核验"标记，按 id 去重防止双算。"""
    rows = list(conn.execute(
        "SELECT m.* FROM material m WHERE m.unit_id=? ORDER BY m.id",
        (unit_id,)).fetchall())
    ids = {r["id"] for r in rows}
    if appointment_id:
        for r in conn.execute(
                """SELECT m.* FROM material_addition ma
                   JOIN material m ON m.id=ma.material_id
                   WHERE ma.appointment_id=?""", (appointment_id,)).fetchall():
            if r["id"] not in ids:
                rows.append(r)
                ids.add(r["id"])
    total = round(sum(r["weight_kg"] for r in rows), 1)
    overlong = int(any(r["overlong"] for r in rows))
    return total, overlong, [dict(r) for r in rows]


def expire_holds(conn, now=None):
    """过期预约自动释放：超时未确认的 held 占位、或时段已结束仍 held。

    幂等，可在每次查询/门岗动作前调用。
    """
    now = _now() if now is None else now
    rows = conn.execute(
        "SELECT id, slot_date, slot_end, hold_expires_at FROM appointment "
        "WHERE status='held'").fetchall()
    released = []
    for r in rows:
        slot_end_epoch = _slot_epoch(r["slot_date"], r["slot_end"])
        if r["hold_expires_at"] is not None and float(r["hold_expires_at"]) <= now:
            reason = "hold_ttl"
        elif slot_end_epoch <= now:
            reason = "slot_passed"
        else:
            continue
        with immediate_tx(conn):
            cur = conn.execute(
                "UPDATE appointment SET status='released', note=COALESCE(note,'')||? "
                "WHERE id=? AND status='held'",
                (f"[自动释放:{reason}]", r["id"]))
            if cur.rowcount:
                conn.execute("DELETE FROM bay_ticket WHERE appointment_id=?", (r["id"],))
                released.append((r["id"], reason))
    return released


def create_appointment(conn, *, unit_id, elevator_id, slot_date, slot_start,
                       plate, vehicle_adapter, note="", now=None):
    """创建预约（held 占位）。校验车辆、载重、梯位与装卸区容量。"""
    now = _now() if now is None else now
    try:
        end = dict(slot_labels(slot_date))[slot_start]
    except KeyError:
        raise BusinessError("不在可预约时段内", "bad_slot")

    unit = conn.execute("SELECT * FROM unit WHERE id=?", (unit_id,)).fetchone()
    if not unit:
        raise BusinessError("装修单元不存在", "no_unit")
    elev = conn.execute("SELECT * FROM elevator WHERE id=?", (elevator_id,)).fetchone()
    if not elev:
        raise BusinessError("电梯不存在", "no_elevator")
    if elev["building_id"] != unit["building_id"]:
        raise BusinessError("电梯与单元不在同一楼栋", "wrong_building")
    if elev["status"] == "fault":
        raise BusinessError("该电梯故障中，暂不可预约", "elevator_fault")

    return _do_create(conn, unit=unit, elev=elev, slot_date=slot_date,
                      slot_start=slot_start, end=end,
                      vehicle_adapter=vehicle_adapter, note=note, now=now,
                      plate=plate)


def _do_create(conn, *, unit, elev, slot_date, slot_start, end,
               vehicle_adapter, note, now, plate):
    veh = vehicle_adapter.get(plate)
    if not veh:
        raise BusinessError("车辆未在运输备案库中", "no_vehicle")

    total, overlong, _ = _unit_material_totals(conn, unit["id"])
    # 重量边界：申报总重恰好等于电梯载重允许，超过则不受理
    if total > elev["load_kg"]:
        raise BusinessError(
            f"申报总重 {total}kg 超过电梯载重 {elev['load_kg']}kg", "overweight")

    building = conn.execute("SELECT * FROM building WHERE id=?",
                            (elev["building_id"],)).fetchone()
    hold_expires = now + config.HOLD_TTL_SECONDS

    try:
        with immediate_tx(conn):
            # 再查一次装卸区容量（拿写锁后计数），满则拒绝
            used = bay_used(conn, building["id"], slot_date, slot_start)
            if used >= building["loading_bays"]:
                raise CapacityError(
                    f"{building['name']} {slot_start} 装卸区车位已约满", "bay_full")

            cur = conn.execute(
                """INSERT INTO appointment
                   (unit_id, building_id, elevator_id, slot_date, slot_start, slot_end,
                    vehicle_plate, driver_name, driver_phone, carrier, status,
                    total_weight_kg, has_overlong, hold_expires_at, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'held', ?, ?, ?, ?)""",
                (unit["id"], building["id"], elev["id"], slot_date, slot_start, end,
                 veh.plate, veh.driver, veh.driver_phone, veh.carrier,
                 total, overlong, str(hold_expires), note))
            appt_id = cur.lastrowid
            # 抢占车位号：唯一约束兜底并发
            bay_no = used + 1
            try:
                conn.execute(
                    """INSERT INTO bay_ticket(building_id, slot_date, slot_start,
                       bay_no, appointment_id) VALUES (?,?,?,?,?)""",
                    (building["id"], slot_date, slot_start, bay_no, appt_id))
            except sqlite3.IntegrityError:
                raise CapacityError("装卸区车位竞争失败", "bay_full")
    except sqlite3.IntegrityError as e:
        # 同电梯同时段唯一索引
        raise CapacityError("该电梯时段已被占用", "elevator_slot_full") from e

    return get_appointment(conn, appt_id)


def get_appointment(conn, appointment_id):
    row = conn.execute("SELECT * FROM appointment WHERE id=?",
                       (appointment_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- 保护确认

def record_protection_checks(conn, appointment_id, checks, checked_by):
    """登记保护点位巡检（含本地照片路径）。checks: [(point_id, present, photo_path|None)]"""
    appt = get_appointment(conn, appointment_id)
    if not appt:
        raise BusinessError("预约不存在", "no_appointment")
    if appt["status"] not in ("held", "confirmed"):
        raise BusinessError("当前状态不可登记保护巡检", "bad_status")
    with immediate_tx(conn):
        for point_id, present, photo_path in checks:
            conn.execute(
                """INSERT INTO protection_check(appointment_id, point_id, present,
                   photo_path, checked_by) VALUES (?,?,?,?,?)
                   ON CONFLICT(appointment_id, point_id) DO UPDATE SET
                     present=excluded.present, photo_path=excluded.photo_path,
                     checked_by=excluded.checked_by,
                     checked_at=datetime('now','localtime')""",
                (appointment_id, point_id, int(bool(present)), photo_path, checked_by))
    return True


def confirm_protection(conn, appointment_id, approved_by, remark="", now=None):
    """物业确认保护方案；全部必设点位到位且批准后 held → confirmed。"""
    now = _now() if now is None else now
    appt = get_appointment(conn, appointment_id)
    if not appt:
        raise BusinessError("预约不存在", "no_appointment")
    if appt["status"] not in ("held", "confirmed"):
        raise BusinessError("仅占位/已确认预约可做保护确认", "bad_status")

    required = conn.execute(
        "SELECT * FROM protection_point WHERE building_id=? AND required=1",
        (appt["building_id"],)).fetchall()
    checks = {r["point_id"]: r for r in conn.execute(
        "SELECT * FROM protection_check WHERE appointment_id=?", (appointment_id,)).fetchall()}
    missing = [p["name"] for p in required
               if p["id"] not in checks or not checks[p["id"]]["present"]]

    approved = 0 if missing else 1
    with immediate_tx(conn):
        conn.execute(
            """INSERT INTO protection_confirmation(appointment_id, approved, approved_by, remark)
               VALUES (?,?,?,?)
               ON CONFLICT(appointment_id) DO UPDATE SET
                 approved=excluded.approved, approved_by=excluded.approved_by,
                 approved_at=datetime('now','localtime'), remark=excluded.remark""",
            (appointment_id, approved, approved_by,
             (remark or "") + ("；缺失:" + "、".join(missing) if missing else "")))
        if missing:
            raise BusinessError("必设保护板缺失：" + "、".join(missing), "no_protection")
        conn.execute(
            """UPDATE appointment SET status='confirmed',
                   confirmed_at=datetime('now','localtime'), hold_expires_at=NULL
               WHERE id=? AND status IN ('held','confirmed')""",
            (appointment_id,))
        # 重新核验通过：临时新增材料标记为已核
        conn.execute("UPDATE material_addition SET rechecked=1 WHERE appointment_id=?",
                     (appointment_id,))
    return get_appointment(conn, appointment_id)


def add_material(conn, appointment_id, *, name, weight_kg, length_cm,
                 width_cm, height_cm, overlong=False):
    """临时增加材料：写入清单并挂到预约，物业必须重新核验保护方案。"""
    appt = get_appointment(conn, appointment_id)
    if not appt:
        raise BusinessError("预约不存在", "no_appointment")
    if appt["status"] not in ("held", "confirmed"):
        raise BusinessError("已放行/已签收预约不能临时加材料", "bad_status")
    with immediate_tx(conn):
        total, _, _ = _unit_material_totals(conn, appt["unit_id"], appointment_id)
        elev = conn.execute("SELECT * FROM elevator WHERE id=?",
                            (appt["elevator_id"],)).fetchone()
        if round(total + weight_kg, 1) > elev["load_kg"]:
            raise BusinessError(
                f"新增后总重 {round(total + weight_kg,1)}kg 超过电梯载重", "overweight")
        cur = conn.execute(
            """INSERT INTO material(unit_id, name, weight_kg, length_cm, width_cm,
               height_cm, overlong) VALUES (?,?,?,?,?,?,?)""",
            (appt["unit_id"], name, weight_kg, length_cm, width_cm, height_cm,
             int(bool(overlong))))
        conn.execute(
            "INSERT INTO material_addition(appointment_id, material_id) VALUES (?,?)",
            (appointment_id, cur.lastrowid))
        total2, overlong2, _ = _unit_material_totals(
            conn, appt["unit_id"], appointment_id)
        conn.execute(
            "UPDATE appointment SET total_weight_kg=?, has_overlong=? WHERE id=?",
            (total2, int(overlong2 or appt["has_overlong"]), appointment_id))
    return get_appointment(conn, appointment_id)


# ---------------------------------------------------------------- 门岗

# 门岗端可见字段白名单（脱敏）：不暴露住户姓名/电话、司机电话、详细地址
GATE_FIELDS = ("id", "vehicle_plate", "carrier", "building_name", "room_no",
               "elevator_code", "slot_date", "slot_start", "slot_end",
               "total_weight_kg", "load_kg", "status", "weigh_kg")


def gate_view(conn, appointment, weigh_kg=None):
    """门岗只读视图：仅必要字段。"""
    b = conn.execute("SELECT name FROM building WHERE id=?",
                     (appointment["building_id"],)).fetchone()
    u = conn.execute("SELECT room_no FROM unit WHERE id=?",
                     (appointment["unit_id"],)).fetchone()
    e = conn.execute("SELECT code, load_kg FROM elevator WHERE id=?",
                     (appointment["elevator_id"],)).fetchone()
    full = dict(appointment)
    full.update(building_name=b["name"], room_no=u["room_no"],
                elevator_code=e["code"], load_kg=e["load_kg"], weigh_kg=weigh_kg)
    return {k: full.get(k) for k in GATE_FIELDS}


def gate_check(conn, *, appointment_id=None, plate=None, gate_building_id=None,
               weighbridge, vehicle_adapter=None, now=None,
               _skip_weigh=False):
    """门岗核验并放行。返回 (view, gate_record)。不满足任一条件即登记 reject。"""
    now = _now() if now is None else now
    expire_holds(conn, now=now)

    if appointment_id is None:
        row = conn.execute(
            """SELECT * FROM appointment WHERE vehicle_plate=?
               AND status IN ('confirmed') ORDER BY id DESC LIMIT 1""",
            (plate,)).fetchone()
        if not row:
            raise BusinessError("找不到该车牌待放行的预约", "no_appointment")
        appointment_id = row["id"]

    with immediate_tx(conn):
        appt_row = conn.execute("SELECT * FROM appointment WHERE id=?",
                                (appointment_id,)).fetchone()
        if not appt_row:
            raise BusinessError("预约不存在", "no_appointment")
        appt = dict(appt_row)

        def reject(code, detail):
            cur = conn.execute(
                """INSERT INTO gate_record(appointment_id, result, issue_code, detail)
                   VALUES (?, 'reject', ?, ?)""",
                (appointment_id, code, detail))
            return gate_view(conn, appt), dict(conn.execute(
                "SELECT * FROM gate_record WHERE id=?", (cur.lastrowid,)).fetchone())

        # 1) 状态机：只处理 confirmed；已放行的重复点击不允许二次成功
        if appt["status"] in ("gate_passed", "arrived", "signed"):
            return reject("already_passed", "该车辆已完成放行，请勿重复操作")
        if appt["status"] != "confirmed":
            return reject("not_confirmed", f"预约状态为 {appt['status']}，尚未通过物业确认")

        # 2) 错楼栋：门岗位置与预约楼栋不符，或车辆未备案进入该楼栋
        if gate_building_id is not None and gate_building_id != appt["building_id"]:
            bn = conn.execute("SELECT name FROM building WHERE id=?",
                              (appt["building_id"],)).fetchone()["name"]
            return reject("wrong_building", f"本预约应前往 {bn}")
        if vehicle_adapter is not None:
            veh = vehicle_adapter.get(appt["vehicle_plate"])
            bldg_no = str(conn.execute(
                "SELECT name FROM building WHERE id=?",
                (appt["building_id"],)).fetchone()["name"]).split("号")[0]
            if veh is None:
                return reject("wrong_building", "车辆未在运输备案库中")
            if bldg_no not in veh.permitted_buildings:
                return reject("wrong_building",
                              f"车牌 {veh.plate} 未备案进入 {bldg_no}号楼")

        # 3) 电梯故障
        elev = conn.execute("SELECT * FROM elevator WHERE id=?",
                            (appt["elevator_id"],)).fetchone()
        if elev["status"] == "fault":
            return reject("elevator_fault", f"{elev['code']} 故障停用")

        # 4) 保护方案：已批准 + 必设点位现场仍在位 + 无未核验的临时新增材料
        pc = conn.execute(
            "SELECT * FROM protection_confirmation WHERE appointment_id=?",
            (appointment_id,)).fetchone()
        if not pc or not pc["approved"]:
            return reject("no_protection", "物业尚未确认保护方案")
        required = conn.execute(
            "SELECT id, name FROM protection_point WHERE building_id=? AND required=1",
            (appt["building_id"],)).fetchall()
        live = {r["point_id"]: r for r in conn.execute(
            "SELECT * FROM protection_check WHERE appointment_id=?", (appointment_id,)).fetchall()}
        missing = [p["name"] for p in required
                   if p["id"] not in live or not live[p["id"]]["present"]]
        if missing:
            return reject("no_protection", "保护板缺失：" + "、".join(missing))
        pending = conn.execute(
            "SELECT COUNT(*) c FROM material_addition WHERE appointment_id=? AND rechecked=0",
            (appointment_id,)).fetchone()["c"]
        if pending:
            return reject("no_protection", f"有 {pending} 项临时新增材料未重新核验")

        # 5) 迟到（超过时段结束 + 宽限）
        slot_end_epoch = _slot_epoch(appt["slot_date"], appt["slot_end"])
        if now > slot_end_epoch + config.LATE_GRACE_MINUTES * 60:
            return reject("late",
                          f"晚于 {appt['slot_end']}（宽限 {config.LATE_GRACE_MINUTES} 分钟）")

        # 6) 地磅称重（模拟适配器）
        weigh_kg = None
        if not _skip_weigh:
            try:
                weigh_kg = weighbridge.weigh(appt["vehicle_plate"],
                                             appt["total_weight_kg"])
            except Exception as exc:  # 适配器故障按电梯/设备故障类登记，不放行
                return reject("weighbridge_fault", str(exc))
            # 重量边界：等于额定载重放行，超过 1g 也算超
            if weigh_kg > elev["load_kg"] + 1e-9:
                return reject("overweight",
                              f"实测 {weigh_kg}kg 超过电梯载重 {elev['load_kg']}kg")

        # 7) 条件更新：并发双击只有一次 rowcount=1
        cur = conn.execute(
            "UPDATE appointment SET status='gate_passed', gate_passed_at=datetime('now','localtime') "
            "WHERE id=? AND status='confirmed'",
            (appointment_id,))
        if cur.rowcount == 0:
            return reject("concurrent_lost", "并发确认冲突，该时段已有车辆完成放行")
        rec_cur = conn.execute(
            """INSERT INTO gate_record(appointment_id, result, issue_code, detail)
               VALUES (?, 'pass', 'none', ?)""",
            (appointment_id,
             f"实测 {weigh_kg}kg" if weigh_kg is not None else "免称重"))
        rec = dict(conn.execute("SELECT * FROM gate_record WHERE id=?",
                                (rec_cur.lastrowid,)).fetchone())

    appt = get_appointment(conn, appointment_id)
    return gate_view(conn, appt, weigh_kg), rec


def mark_arrived(conn, appointment_id):
    """车辆到场卸货确认。已签收材料不能回退为未到场。"""
    with immediate_tx(conn):
        cur = conn.execute(
            "UPDATE appointment SET status='arrived', arrived_at=datetime('now','localtime') "
            "WHERE id=? AND status='gate_passed'",
            (appointment_id,))
        if cur.rowcount == 0:
            st = get_appointment(conn, appointment_id)["status"]
            if st == "signed":
                raise BusinessError("已签收材料不能回退为未到场", "signed_locked")
            raise BusinessError(f"当前状态 {st} 不可登记到场", "bad_status")
    return get_appointment(conn, appointment_id)


# ---------------------------------------------------------------- 签收

def signoff(conn, appointment_id, signed_by, items, note=""):
    """签收并登记差异。items: [(material_id, received_qty, received_weight_kg)]

    幂等：同一预约重复提交直接返回首条记录 (..., idempotent=True)，状态不再变更。
    """
    appt = get_appointment(conn, appointment_id)
    if not appt:
        raise BusinessError("预约不存在", "no_appointment")
    existing = conn.execute("SELECT * FROM signoff WHERE appointment_id=?",
                            (appointment_id,)).fetchone()
    if existing:
        return _signoff_payload(conn, existing["id"], idempotent=True)

    if appt["status"] not in ("gate_passed", "arrived"):
        raise BusinessError("仅已放行车辆可签收", "bad_status")

    with immediate_tx(conn):
        cur = conn.execute(
            "INSERT INTO signoff(appointment_id, signed_by, note) VALUES (?,?,?)",
            (appointment_id, signed_by, note))
        sid = cur.lastrowid

        declared = {r["id"]: dict(r) for r in conn.execute(
            """SELECT m.* FROM material m WHERE m.unit_id=?
               UNION
               SELECT m.* FROM material_addition ma JOIN material m ON m.id=ma.material_id
               WHERE ma.appointment_id=?""",
            (appt["unit_id"], appointment_id))}
        submitted = {mid: (qty, w) for mid, qty, w in items}
        for mid, m in declared.items():
            qty, w = submitted.get(mid, (None, None))
            if qty is None and w is None:
                code = "short"
            elif qty is not None and qty < 1:
                code = "short"
            elif w is not None and w + 1e-9 < m["weight_kg"] * max(qty or 1, 1) * 0.999:
                code = "short"
            elif w is not None and w > m["weight_kg"] * max(qty or 1, 1) * 1.001 + 1e-9:
                code = "extra"
            else:
                code = "ok"
            conn.execute(
                """INSERT INTO signoff_item(signoff_id, material_id, declared_weight_kg,
                   received_weight_kg, declared_qty, received_qty, diff_code)
                   VALUES (?,?,?,?,1,?,?)""",
                (sid, mid, m["weight_kg"], w, qty, code))

        # 有损坏可在 note 注明；状态机：signed 为终态，不可回退
        conn.execute(
            "UPDATE appointment SET status='signed', signed_at=datetime('now','localtime') "
            "WHERE id=? AND status IN ('gate_passed','arrived')",
            (appointment_id,))
    return _signoff_payload(conn, sid, idempotent=False)


def _signoff_payload(conn, signoff_id, idempotent):
    s = dict(conn.execute("SELECT * FROM signoff WHERE id=?", (signoff_id,)).fetchone())
    items = [dict(r) for r in conn.execute(
        """SELECT si.*, m.name material_name FROM signoff_item si
           JOIN material m ON m.id=si.material_id WHERE si.signoff_id=?""",
        (signoff_id,)).fetchall()]
    return {"signoff": s, "items": items, "idempotent": idempotent}


def list_signoff_diffs(conn):
    return [dict(r) for r in conn.execute(
        """SELECT si.diff_code, si.received_weight_kg, si.declared_weight_kg,
                  si.received_qty, m.name material_name,
                  a.vehicle_plate, b.name building_name, u.room_no, s.signed_at, s.signed_by
           FROM signoff_item si
           JOIN signoff s ON s.id=si.signoff_id
           JOIN appointment a ON a.id=s.appointment_id
           JOIN building b ON b.id=a.building_id
           JOIN unit u ON u.id=a.unit_id
           JOIN material m ON m.id=si.material_id
           WHERE si.diff_code != 'ok'
           ORDER BY s.signed_at DESC""")]
