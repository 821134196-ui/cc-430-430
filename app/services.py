"""领域服务：材料清单、预约容量、保护确认、门岗放行、签收、异常。

所有会改变容量占用或产生放行/签收记录的写操作都在单条
``BEGIN IMMEDIATE`` 事务内完成，保证并发下只有一个成功。
时间通过参数注入（now），便于测试占位过期与迟到判定。
"""
import json
import secrets
from datetime import datetime, timedelta

from . import adapters, config


class DomainError(Exception):
    """业务规则错误，message 可直接展示给用户。"""


class CapacityError(DomainError):
    pass


class NotFound(DomainError):
    pass


# 状态机：只允许向前流转，signed/expired/cancelled 为终态。
# “已签收材料不能回退为未到场”由该表保证：signed 没有任何出边。
_TRANSITIONS = {
    config.ST_HELD: {config.ST_PROTECTED, config.ST_EXPIRED, config.ST_CANCELLED},
    config.ST_PROTECTED: {config.ST_PASSED, config.ST_EXPIRED, config.ST_CANCELLED},
    config.ST_PASSED: {config.ST_SIGNED},
    config.ST_SIGNED: set(),
    config.ST_EXPIRED: set(),
    config.ST_CANCELLED: set(),
}

SLOTS = [("08:00", "10:00"), ("10:00", "12:00"), ("13:00", "15:00"), ("15:00", "17:00")]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _set_status(conn, appt_id, new_status, now, **extra):
    row = conn.execute("SELECT status FROM appointment WHERE id=?", (appt_id,)).fetchone()
    if not row:
        raise NotFound("预约不存在")
    cur = row["status"]
    if new_status not in _TRANSITIONS.get(cur, set()):
        raise DomainError(f"预约状态不允许从 {cur} 变更为 {new_status}（已签收/终态不可回退）")
    sets = ", ".join(["status=?"] + [f"{k}=?" for k in extra])
    conn.execute(
        f"UPDATE appointment SET {sets} WHERE id=?",
        (new_status, *extra.values(), appt_id),
    )


def _log_exception(conn, appointment_id, kind, detail, now):
    conn.execute(
        "INSERT INTO exception_log(appointment_id, kind, detail, created_at) VALUES(?,?,?,?)",
        (appointment_id, kind, detail, _iso(now)),
    )


def _iso(dt):
    return dt.isoformat(timespec="seconds") if isinstance(dt, datetime) else dt


# ---------------------------------------------------------------- 材料清单

def _recompute_list(conn, list_id, now):
    items = conn.execute(
        "SELECT * FROM material_item WHERE list_id=? AND removed=0", (list_id,)
    ).fetchall()
    total = round(sum(i["weight_kg"] for i in items), 2)
    overlong = 1 if any(i["overlong"] for i in items) else 0
    conn.execute(
        "UPDATE material_list SET total_weight=?, has_overlong=?, updated_at=? WHERE id=?",
        (total, overlong, _iso(now), list_id),
    )
    return total, overlong


def create_material_list(conn, unit_id, items, now):
    """items: [{name, qty, unit, weight_kg, length_m, width_m, height_m}]"""
    cur = conn.execute(
        "INSERT INTO material_list(unit_id, status, updated_at) VALUES(?,?,?)",
        (unit_id, "verified", _iso(now)),
    )
    list_id = cur.lastrowid
    for it in items:
        add_material_item(conn, list_id, it, now, bump_version=False)
    _recompute_list(conn, list_id, now)
    return list_id


def add_material_item(conn, list_id, it, now, bump_version=True):
    """临时增加材料：清单版本 +1 并置为 recheck（必须重新核验）。"""
    dims = [float(it.get(k) or 0) for k in ("length_m", "width_m", "height_m")]
    overlong = 1 if max(dims, default=0) >= config.OVERLONG_M else 0
    conn.execute(
        """INSERT INTO material_item(list_id,name,qty,unit,weight_kg,length_m,width_m,height_m,overlong)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (list_id, it["name"], float(it.get("qty", 1)), it.get("unit", "件"),
         float(it["weight_kg"]), *dims, overlong),
    )
    if bump_version:
        conn.execute(
            "UPDATE material_list SET version=version+1, status='recheck' WHERE id=?",
            (list_id,),
        )
    _recompute_list(conn, list_id, now)


def remove_material_item(conn, list_id, item_id, now):
    conn.execute(
        "UPDATE material_item SET removed=1 WHERE id=? AND list_id=?", (item_id, list_id)
    )
    conn.execute(
        "UPDATE material_list SET version=version+1, status='recheck' WHERE id=?", (list_id,)
    )
    _recompute_list(conn, list_id, now)


def verify_material_list(conn, list_id, now):
    """物业重新核验清单：recheck -> verified。

    同步刷新该单元处于 held 状态预约的材料快照（版本/重量/超长），
    刷新前复查电梯载重；超重则拒绝核验，须先调整清单。
    """
    lst = conn.execute("SELECT * FROM material_list WHERE id=?", (list_id,)).fetchone()
    if not lst:
        raise NotFound("材料清单不存在")
    appts = conn.execute(
        """SELECT a.*, e.max_load_kg FROM appointment a
           JOIN elevator e ON e.id=a.elevator_id
           WHERE a.unit_id=? AND a.status=?""",
        (lst["unit_id"], config.ST_HELD)).fetchall()
    for a in appts:
        if lst["total_weight"] > a["max_load_kg"]:
            raise DomainError(
                f"变更后总重 {lst['total_weight']}kg 超过电梯载重 {a['max_load_kg']}kg，"
                "请先调整材料清单")
    conn.execute(
        "UPDATE material_list SET status='verified', updated_at=? WHERE id=?",
        (_iso(now), list_id),
    )
    for a in appts:
        conn.execute(
            """UPDATE appointment SET list_version=?, list_weight=?, list_overlong=?
               WHERE id=?""",
            (lst["version"], lst["total_weight"], lst["has_overlong"], a["id"]))


def list_detail(conn, list_id):
    lst = conn.execute("SELECT * FROM material_list WHERE id=?", (list_id,)).fetchone()
    if not lst:
        raise NotFound("材料清单不存在")
    items = conn.execute(
        "SELECT * FROM material_item WHERE list_id=? AND removed=0 ORDER BY id", (list_id,)
    ).fetchall()
    return {"list": dict(lst), "items": [dict(i) for i in items]}


def unit_active_list(conn, unit_id):
    row = conn.execute(
        "SELECT id FROM material_list WHERE unit_id=? ORDER BY id DESC LIMIT 1", (unit_id,)
    ).fetchone()
    return row["id"] if row else None


# ---------------------------------------------------------------- 容量与预约

def _resources(conn, building_id, elevator_id, dock_id):
    b = conn.execute("SELECT * FROM building WHERE id=?", (building_id,)).fetchone()
    if not b:
        raise NotFound("楼栋不存在")
    return [
        (f"bldg:{building_id}", b["dock_count"]),   # 楼栋装卸区总容量
        (f"elev:{elevator_id}", 1),                  # 每台电梯同时段 1 车
        (f"dock:{dock_id}", 1),                      # 每个装卸车位同时段 1 车
    ]


def _occupy(conn, resources, slot_date, slot_start, slot_end):
    """在写事务内占用名额；任一资源满则抛 CapacityError。"""
    for res, cap in resources:
        conn.execute(
            """INSERT INTO slot_occupancy(resource,slot_date,slot_start,slot_end,capacity,used)
               VALUES(?,?,?,?,?,0)
               ON CONFLICT(resource,slot_date,slot_start) DO NOTHING""",
            (res, slot_date, slot_start, slot_end, cap),
        )
        cur = conn.execute(
            """UPDATE slot_occupancy SET used=used+1
               WHERE resource=? AND slot_date=? AND slot_start=? AND used<capacity""",
            (res, slot_date, slot_start),
        )
        if cur.rowcount == 0:
            raise CapacityError(f"时段 {slot_date} {slot_start} 名额已满（{res}）")


def _release(conn, resources, slot_date, slot_start):
    for res, _cap in resources:
        conn.execute(
            """UPDATE slot_occupancy SET used=MAX(used-1,0)
               WHERE resource=? AND slot_date=? AND slot_start=?""",
            (res, slot_date, slot_start),
        )


def create_appointment(conn, building_id, elevator_id, dock_id, unit_id,
                       plate, driver, slot_date, slot_start, list_id, now):
    """创建预约（held 占位）。容量检查与占用在同一写事务内完成。"""
    slot_end = dict(SLOTS).get(slot_start)
    if not slot_end:
        raise DomainError("非法时段")
    plate = adapters.normalize_plate(plate)
    detail = list_detail(conn, list_id)
    if detail["list"]["status"] != "verified":
        raise DomainError("材料清单未核验，不能预约")
    if detail["list"]["unit_id"] != unit_id:
        raise DomainError("材料清单不属于该装修单元")
    elev = conn.execute(
        "SELECT * FROM elevator WHERE id=? AND building_id=?", (elevator_id, building_id)
    ).fetchone()
    if not elev:
        raise NotFound("电梯不存在")
    if elev["status"] == "fault":
        raise DomainError("电梯故障，暂不可预约")
    if detail["list"]["total_weight"] > elev["max_load_kg"]:
        raise DomainError(
            f"材料总重 {detail['list']['total_weight']}kg 超过电梯载重 {elev['max_load_kg']}kg")

    resources = _resources(conn, building_id, elevator_id, dock_id)
    hold_expires = now + timedelta(minutes=config.HOLD_MINUTES)
    code = "AP" + now.strftime("%Y%m%d") + secrets.token_hex(3).upper()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _occupy(conn, resources, slot_date, slot_start, slot_end)
        cur = conn.execute(
            """INSERT INTO appointment(code,building_id,elevator_id,dock_id,unit_id,plate,driver,
                   slot_date,slot_start,slot_end,status,list_version,list_weight,list_overlong,
                   hold_expires,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, building_id, elevator_id, dock_id, unit_id, plate, driver,
             slot_date, slot_start, slot_end, config.ST_HELD,
             detail["list"]["version"], detail["list"]["total_weight"],
             detail["list"]["has_overlong"], _iso(hold_expires), _iso(now)),
        )
        appt_id = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_appointment(conn, appt_id)


def get_appointment(conn, appt_id):
    row = conn.execute("SELECT * FROM appointment WHERE id=?", (appt_id,)).fetchone()
    if not row:
        raise NotFound("预约不存在")
    return dict(row)


def release_expired(conn, now):
    """过期预约自动释放占用：
    - held：超过占位保留时限（物业未确认保护方案）
    - protected：时段已结束仍未放行到场
    """
    held_rows = conn.execute(
        "SELECT * FROM appointment WHERE status=? AND hold_expires < ?",
        (config.ST_HELD, _iso(now)),
    ).fetchall()
    cutoff = _iso(now)
    prot_rows = [
        r for r in conn.execute(
            "SELECT * FROM appointment WHERE status=?", (config.ST_PROTECTED,)).fetchall()
        if f"{r['slot_date']}T{r['slot_end']}" < cutoff
    ]
    n = 0
    for r in held_rows + prot_rows:
        conn.execute("BEGIN IMMEDIATE")
        try:
            resources = _resources(conn, r["building_id"], r["elevator_id"], r["dock_id"])
            _release(conn, resources, r["slot_date"], r["slot_start"])
            _set_status(conn, r["id"], config.ST_EXPIRED, now)
            conn.execute("COMMIT")
            n += 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return n


def cancel_appointment(conn, appt_id, now):
    r = get_appointment(conn, appt_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        resources = _resources(conn, r["building_id"], r["elevator_id"], r["dock_id"])
        _release(conn, resources, r["slot_date"], r["slot_start"])
        _set_status(conn, appt_id, config.ST_CANCELLED, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- 电梯保护

def protection_points(conn, building_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM protection_point WHERE building_id=? ORDER BY id", (building_id,))]


def confirm_protection(conn, appt_id, checks, confirmed_by, now):
    """物业确认保护方案。checks: {point_id: {"ok": bool, "photo": bytes|None}}

    所有必检点必须 ok 且有照片；确认后预约进入 protected 可放行状态。
    若材料清单在预约后被临时增改（版本不一致或 recheck），拒绝确认。
    """
    appt = get_appointment(conn, appt_id)
    if appt["status"] != config.ST_HELD:
        raise DomainError(f"当前状态 {appt['status']} 不能确认保护方案")
    lst = conn.execute(
        """SELECT ml.* FROM material_list ml
           JOIN unit u ON u.id=ml.unit_id WHERE u.id=? ORDER BY ml.id DESC LIMIT 1""",
        (appt["unit_id"],),
    ).fetchone()
    if lst and (lst["version"] != appt["list_version"] or lst["status"] == "recheck"):
        raise DomainError("材料清单已临时变更，必须重新核验后才能确认保护方案")

    points = protection_points(conn, appt["building_id"])
    missing = [p["name"] for p in points
               if p["required"] and not checks.get(p["id"], {}).get("ok")]
    if missing:
        raise DomainError("必检保护点未完成：" + "、".join(missing))

    conn.execute("BEGIN IMMEDIATE")
    try:
        for p in points:
            c = checks.get(p["id"], {})
            photo_rel = None
            if c.get("ok"):
                photo_rel = adapters.save_photo(appt_id, p["id"], c.get("photo"))
            conn.execute(
                """INSERT INTO protection_check(appointment_id,point_id,ok,photo_path,checked_by,checked_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(appointment_id,point_id) DO UPDATE SET
                     ok=excluded.ok, photo_path=excluded.photo_path,
                     checked_by=excluded.checked_by, checked_at=excluded.checked_at""",
                (appt_id, p["id"], 1 if c.get("ok") else 0, photo_rel,
                 confirmed_by, _iso(now)),
            )
        _set_status(conn, appt_id, config.ST_PROTECTED, now, confirmed_at=_iso(now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def protection_status(conn, appt_id):
    appt = get_appointment(conn, appt_id)
    points = protection_points(conn, appt["building_id"])
    checks = {r["point_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM protection_check WHERE appointment_id=?", (appt_id,))}
    out = []
    for p in points:
        c = checks.get(p["id"])
        out.append({**p, "ok": bool(c and c["ok"]),
                    "photo_path": c and c["photo_path"]})
    return out


# ---------------------------------------------------------------- 门岗放行

def gate_view(conn, appt_id):
    """门岗端视图：只暴露放行所需字段（脱敏，不含住户姓名/电话/房号）。"""
    appt = get_appointment(conn, appt_id)
    b = conn.execute("SELECT name FROM building WHERE id=?", (appt["building_id"],)).fetchone()
    e = conn.execute("SELECT code, max_load_kg, status FROM elevator WHERE id=?",
                     (appt["elevator_id"],)).fetchone()
    return {
        "code": appt["code"],
        "plate": appt["plate"],
        "driver": appt["driver"],
        "building": b["name"],
        "elevator": e["code"],
        "slot": f"{appt['slot_date']} {appt['slot_start']}-{appt['slot_end']}",
        "status": appt["status"],
        "list_weight": appt["list_weight"],
        "list_overlong": bool(appt["list_overlong"]),
    }


def gate_pass(conn, plate, building_id, guard, now, weighbridge=None):
    """门岗核验放行。返回 {passed, reason, appointment_id, view, exceptions}。

    规则：
    - 车牌在别楼栋有有效预约 → 错楼栋异常，拒绝
    - 电梯故障 → 异常，拒绝
    - 必检保护点缺失 → 异常，拒绝
    - 超过时段开始+LATE_GRACE → 迟到异常，拒绝
    - 地磅实称货物 > 电梯载重 → 超重异常，拒绝
    - 并发：gate_pass.appointment_id 唯一约束 + 写事务，只放行一次
    """
    weighbridge = weighbridge or adapters.WeighbridgeAdapter()
    plate = adapters.normalize_plate(plate)
    result = {"passed": False, "reason": "", "appointment_id": None,
              "view": None, "exceptions": []}

    rows = conn.execute(
        """SELECT * FROM appointment
           WHERE plate=? AND status IN (?,?,?) ORDER BY id DESC""",
        (plate, config.ST_HELD, config.ST_PROTECTED, config.ST_PASSED),
    ).fetchall()
    here = next((dict(r) for r in rows if r["building_id"] == building_id), None)
    other = next((dict(r) for r in rows if r["building_id"] != building_id), None)
    if not here and not other:
        result["reason"] = "无有效预约"
        return result
    appt = here or other
    result["appointment_id"] = appt["id"]
    result["view"] = gate_view(conn, appt["id"])

    def deny(kind, reason):
        _log_exception(conn, appt["id"], kind, reason, now)
        result["exceptions"].append(kind)
        result["reason"] = reason

    if here is None and other is not None:
        b = conn.execute("SELECT name FROM building WHERE id=?",
                         (appt["building_id"],)).fetchone()
        deny(config.EX_WRONG_BUILDING,
             f"该车牌预约的是 {b['name']}，非本楼栋，错楼栋禁止放行")
        return result
    if appt["status"] == config.ST_PASSED:
        result["reason"] = "该预约已放行"
        return result
    if appt["status"] != config.ST_PROTECTED:
        result["reason"] = "物业尚未确认保护方案，不能放行"
        return result

    elev = conn.execute("SELECT * FROM elevator WHERE id=?", (appt["elevator_id"],)).fetchone()
    if elev["status"] == "fault":
        deny(config.EX_ELEVATOR_FAULT, f"电梯 {elev['code']} 故障，暂停放行")
        return result

    slot_start_dt = datetime.fromisoformat(f"{appt['slot_date']}T{appt['slot_start']}")
    if now > slot_start_dt + timedelta(minutes=config.LATE_GRACE_MINUTES):
        deny(config.EX_LATE,
             f"预约时段 {appt['slot_start']} 开始，迟到超过 {config.LATE_GRACE_MINUTES} 分钟")
        return result

    missing = [p for p in protection_status(conn, appt["id"])
               if p["required"] and not (p["ok"] and p["photo_path"])]
    if missing:
        deny(config.EX_MISSING_PROTECTION,
             "保护板缺失：" + "、".join(p["name"] for p in missing))
        return result

    _gross, goods = weighbridge.weigh(plate, appt["list_weight"])
    result["weighed_kg"] = goods
    if goods > elev["max_load_kg"]:
        deny(config.EX_OVERWEIGHT,
             f"实称 {goods}kg 超过电梯载重 {elev['max_load_kg']}kg")
        return result

    conn.execute("BEGIN IMMEDIATE")
    try:
        _set_status(conn, appt["id"], config.ST_PASSED, now, passed_at=_iso(now))
        conn.execute(
            """INSERT INTO gate_pass(appointment_id,plate,guard,passed,reason,weighed_kg,created_at)
               VALUES(?,?,?,1,NULL,?,?)""",
            (appt["id"], plate, guard, goods, _iso(now)),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    result["passed"] = True
    result["reason"] = "放行"
    return result


# ---------------------------------------------------------------- 签收

def signoff(conn, appt_id, receiver, actual_items, now):
    """签收。幂等：同一预约重复签收返回首次记录，不重复生成。

    actual_items: [{name, qty, unit}] 实收明细；与清单快照比对，差异登记异常。
    """
    appt = get_appointment(conn, appt_id)
    existing = conn.execute(
        "SELECT * FROM signoff WHERE appointment_id=?", (appt_id,)).fetchone()
    if existing:
        return {"signoff": dict(existing), "idempotent": True,
                "items": _signoff_items(conn, existing["id"])}
    if appt["status"] != config.ST_PASSED:
        raise DomainError(f"预约状态 {appt['status']} 不能签收（须先放行进场）")

    lst = conn.execute(
        """SELECT ml.id FROM material_list ml WHERE ml.unit_id=?
           ORDER BY ml.id DESC LIMIT 1""", (appt["unit_id"],)).fetchone()
    expected = conn.execute(
        "SELECT name, qty, unit FROM material_item WHERE list_id=? AND removed=0",
        (lst["id"],)).fetchall()
    actual_map = {a["name"]: a for a in actual_items}
    diffs, rows = [], []
    for e in expected:
        a = actual_map.get(e["name"])
        aqty = float(a["qty"]) if a else 0.0
        rows.append((e["name"], e["qty"], aqty, e["unit"]))
        if a is None:
            diffs.append(f"{e['name']} 未到货（应 {e['qty']}{e['unit']}）")
        elif aqty != e["qty"]:
            diffs.append(f"{e['name']} 数量不符（应 {e['qty']} 实 {aqty}{e['unit']}）")
    matched = 0 if diffs else 1

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            """INSERT INTO signoff(appointment_id,receiver,matched,diff_detail,created_at)
               VALUES(?,?,?,?,?)""",
            (appt_id, receiver, matched, json.dumps(diffs, ensure_ascii=False) or None,
             _iso(now)),
        )
        sid = cur.lastrowid
        for name, eq, aq, unit in rows:
            conn.execute(
                """INSERT INTO signoff_item(signoff_id,name,expected_qty,actual_qty,unit)
                   VALUES(?,?,?,?,?)""",
                (sid, name, eq, aq, unit))
        _set_status(conn, appt_id, config.ST_SIGNED, now, signed_at=_iso(now))
        if diffs:
            _log_exception(conn, appt_id, config.EX_SIGNOFF_DIFF, "；".join(diffs), now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"signoff": dict(conn.execute(
        "SELECT * FROM signoff WHERE id=?", (sid,)).fetchone()),
        "idempotent": False, "items": _signoff_items(conn, sid)}


def _signoff_items(conn, signoff_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM signoff_item WHERE signoff_id=? ORDER BY id", (signoff_id,))]


# ---------------------------------------------------------------- 查询

def calendar(conn, building_id, slot_date):
    """楼栋日历：时段 × 电梯分栏。"""
    elevs = [dict(r) for r in conn.execute(
        "SELECT * FROM elevator WHERE building_id=? ORDER BY code", (building_id,))]
    appts = [dict(r) for r in conn.execute(
        """SELECT a.*, u.room_no FROM appointment a JOIN unit u ON u.id=a.unit_id
           WHERE a.building_id=? AND a.slot_date=?""", (building_id, slot_date))]
    grid = {e["id"]: {s[0]: [] for s in SLOTS} for e in elevs}
    for a in appts:
        grid.setdefault(a["elevator_id"], {}).setdefault(a["slot_start"], []).append(a)
    return {"elevators": elevs, "slots": SLOTS, "grid": grid, "appointments": appts}


def exceptions(conn, appt_id=None, limit=200):
    if appt_id:
        rows = conn.execute(
            "SELECT * FROM exception_log WHERE appointment_id=? ORDER BY id DESC",
            (appt_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM exception_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
