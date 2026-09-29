"""一键样例：重置演示库 → 预约 → 物业保护确认 → 门岗放行 → 签收，
并额外登记迟到、超重、保护板缺失、电梯故障、错楼栋等异常样例。

运行：python -m app demo
"""
import time
from datetime import datetime

from . import config, photos, services
from .adapters import VehicleAdapter, WeighbridgeAdapter
from .db import connect, init_db


def _reset():
    for p in (config.DB_PATH,):
        if p.exists():
            p.unlink()
    for suffix in ("-wal", "-shm"):
        q = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
        if q.exists():
            q.unlink()
    if config.PHOTO_DIR.exists():
        for f in config.PHOTO_DIR.glob("*"):
            f.unlink()


def _ids(conn):
    bid = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM building")}
    eid = {}
    for r in conn.execute("SELECT id, building_id, code FROM elevator"):
        bn = conn.execute("SELECT name FROM building WHERE id=?",
                          (r["building_id"],)).fetchone()["name"]
        eid[(bn, r["code"])] = r["id"]
    uid = {r["room_no"]: r["id"] for r in conn.execute("SELECT id, room_no FROM unit")}
    return bid, eid, uid


def _approve(conn, appt_id, by="物业·王敏", with_photos=True):
    """模拟物业：点位拍照/勾选 → 确认保护方案。"""
    pts = conn.execute(
        "SELECT p.id, p.name FROM protection_point p JOIN appointment a ON a.building_id=p.building_id "
        "WHERE a.id=? AND p.required=1", (appt_id,)).fetchall()
    checks = []
    for p in pts:
        path = photos.capture_demo_photo(appt_id, p["id"], p["name"]) if with_photos else None
        checks.append((p["id"], 1, path))
    services.record_protection_checks(conn, appt_id, checks, by)
    return services.confirm_protection(conn, appt_id, by)


def run_demo(serve_after=True):
    from .seed import seed
    _reset()
    conn = connect()
    init_db(conn)
    seed(conn, verbose=False)
    vehicles = VehicleAdapter()
    bid, eid, uid = _ids(conn)
    today = datetime.now().strftime("%Y-%m-%d")

    print("=" * 64)
    print("装修材料进场预约与电梯保护确认系统 —— 样例数据生成")
    print("=" * 64)

    # 1) 正常全流程：1号楼 1-1202，货梯，09:00
    a1 = services.create_appointment(
        conn, unit_id=uid["1-1202"], elevator_id=eid[("1号楼", "1#货梯")],
        slot_date=today, slot_start="09:00", plate="沪D·Q1046",
        vehicle_adapter=vehicles, note="木地板与龙骨进场")
    _approve(conn, a1["id"])
    print(f"[1] 预约 #{a1['id']} 1号楼1-1202 09:00 沪D·Q1046 —— 保护确认通过")
    view, rec = services.gate_check(
        conn, appointment_id=a1["id"], gate_building_id=bid["1号楼"],
        weighbridge=WeighbridgeAdapter("exact"), vehicle_adapter=vehicles)
    print(f"    门岗：{rec['result']} / {rec['detail']}（门岗视图字段：{sorted(view)}）")
    services.mark_arrived(conn, a1["id"])
    _, _, mats = services._unit_material_totals(conn, a1["unit_id"], a1["id"])
    items = [(m["id"], 1, m["weight_kg"]) for m in mats]
    # 模拟其中白乳胶少 1 桶（15kg）
    latex = [m for m in mats if "白乳胶" in m["name"]][0]
    items = [(mid, q, (w - 15 if mid == latex["id"] else w)) for mid, q, w in items]
    payload = services.signoff(conn, a1["id"], "演示住户·林一", items, note="白乳胶实收5桶")
    again = services.signoff(conn, a1["id"], "演示住户·林一", items)
    print(f"    签收：差异 {[i['diff_code'] for i in payload['items']]}；"
          f"重复签收幂等={again['idempotent']}")

    # 2) 保护板缺失：2号楼 2-0501，只登记部分点位，确认被拒
    a2 = services.create_appointment(
        conn, unit_id=uid["2-0501"], elevator_id=eid[("2号楼", "2#货梯")],
        slot_date=today, slot_start="10:00", plate="沪B·K7751",
        vehicle_adapter=vehicles)
    pts = conn.execute(
        "SELECT id FROM protection_point WHERE building_id=? AND required=1",
        (bid["2号楼"],)).fetchall()
    services.record_protection_checks(conn, a2["id"], [(pts[0]["id"], 1, None)], "物业·王敏")
    try:
        services.confirm_protection(conn, a2["id"], "物业·王敏")
    except services.BusinessError as ex:
        print(f"[2] 预约 #{a2['id']} 保护确认被拒：{ex}")
    # 强行把状态置为 confirmed 来演练门岗端的二次拦截（模拟绕过物业页的异常尝试）
    conn.execute("UPDATE appointment SET status='confirmed' WHERE id=?", (a2["id"],))
    conn.execute(
        "INSERT INTO protection_confirmation(appointment_id, approved, approved_by) "
        "VALUES (?,0,'物业·王敏') ON CONFLICT(appointment_id) DO UPDATE SET approved=0",
        (a2["id"],))
    _, rec = services.gate_check(
        conn, appointment_id=a2["id"], gate_building_id=bid["2号楼"],
        weighbridge=WeighbridgeAdapter("exact"), vehicle_adapter=vehicles)
    print(f"    门岗登记：{rec['result']} / {rec['issue_code']} / {rec['detail']}")

    # 3) 错楼栋：3号楼预约车辆开到 1号楼门岗（放行前演练）
    a3 = services.create_appointment(
        conn, unit_id=uid["3-1601"], elevator_id=eid[("3号楼", "3#货梯")],
        slot_date=today, slot_start="10:30", plate="沪D·Q1046",
        vehicle_adapter=vehicles)
    _approve(conn, a3["id"])
    _, rec = services.gate_check(
        conn, appointment_id=a3["id"], gate_building_id=bid["1号楼"],
        weighbridge=WeighbridgeAdapter("exact"), vehicle_adapter=vehicles)
    print(f"[3] 错楼栋登记：{rec['result']} / {rec['issue_code']} / {rec['detail']}")

    # 4) 电梯故障：3#货梯置故障后核验
    conn.execute("UPDATE elevator SET status='fault' WHERE id=?",
                 (eid[("3号楼", "3#货梯")],))
    _, rec = services.gate_check(
        conn, appointment_id=a3["id"], gate_building_id=bid["3号楼"],
        weighbridge=WeighbridgeAdapter("exact"), vehicle_adapter=vehicles)
    print(f"[4] 电梯故障登记：{rec['result']} / {rec['issue_code']} / {rec['detail']}")
    conn.execute("UPDATE elevator SET status='normal' WHERE id=?",
                 (eid[("3号楼", "3#货梯")],))

    # 5) 超重：临时补料使申报总重 1595kg（贴近 1600kg 边界），
    #    物业重新核验后，地磅报超 120kg → 1715kg，门岗以超重拒绝
    services.add_material(conn, a3["id"], name="补送整包龙骨",
                          weight_kg=1000, length_cm=250, width_cm=30,
                          height_cm=20, overlong=False)
    _approve(conn, a3["id"])
    a3 = services.get_appointment(conn, a3["id"])
    _, rec = services.gate_check(
        conn, appointment_id=a3["id"], gate_building_id=bid["3号楼"],
        weighbridge=WeighbridgeAdapter("overweight", force_extra_kg=120),
        vehicle_adapter=vehicles)
    print(f"[5] 超重登记：申报 {a3['total_weight_kg']:g}kg，{rec['result']} / "
          f"{rec['issue_code']} / {rec['detail']}")
    # 边界：精确称重（等于申报 1595 ≤ 1600）可放行
    view, rec = services.gate_check(
        conn, appointment_id=a3["id"], gate_building_id=bid["3号楼"],
        weighbridge=WeighbridgeAdapter("exact"), vehicle_adapter=vehicles)
    print(f"    边界复测：{rec['result']} / {rec['detail']}")

    # 6) 迟到：预约 13:30 时段，门岗在 14:30 核验（超过 14:00 时段结束+10 分钟宽限）
    a6 = services.create_appointment(
        conn, unit_id=uid["3-1601"], elevator_id=eid[("3号楼", "3#货梯")],
        slot_date=today, slot_start="13:30", plate="沪D·Q1046",
        vehicle_adapter=vehicles, note="补送材料，演练迟到")
    _approve(conn, a6["id"])
    late_id = a6["id"]
    late_bid = bid["3号楼"]
    future = datetime.strptime(f"{today} 14:30", "%Y-%m-%d %H:%M").timestamp()
    _, rec = services.gate_check(
        conn, appointment_id=late_id, gate_building_id=late_bid,
        weighbridge=WeighbridgeAdapter("exact"), vehicle_adapter=vehicles, now=future)
    print(f"[6] 迟到登记：{rec['result']} / {rec['issue_code']} / {rec['detail']}")

    # 7) 占位过期：新建一条 held，注入过期 hold_expires_at，触发自动释放
    a7 = services.create_appointment(
        conn, unit_id=uid["2-0501"], elevator_id=eid[("2号楼", "2#客梯")],
        slot_date=today, slot_start="15:00", plate="沪B·K7751",
        vehicle_adapter=vehicles)
    conn.execute("UPDATE appointment SET hold_expires_at=? WHERE id=?",
                 (str(time.time() - 1), a7["id"]))
    released = services.expire_holds(conn)
    print(f"[7] 过期释放：{released}（预约 #{a7['id']} 时段已回收，可被重新预约）")
    a7b = services.create_appointment(
        conn, unit_id=uid["2-0501"], elevator_id=eid[("2号楼", "2#客梯")],
        slot_date=today, slot_start="15:00", plate="沪E·55T88",
        vehicle_adapter=vehicles)
    print(f"    释放后同梯同时段重新预约成功：#{a7b['id']}")

    # 8) 临时加材料须复核：a2 现在状态为 confirmed（门岗被拒过），加材料后再核验
    services.add_material(conn, a2["id"], name="补送石膏板 10 张",
                          weight_kg=250, length_cm=240, width_cm=120,
                          height_cm=1, overlong=True)
    _, rec = services.gate_check(
        conn, appointment_id=a2["id"], gate_building_id=bid["2号楼"],
        weighbridge=WeighbridgeAdapter("exact"), vehicle_adapter=vehicles)
    print(f"[8] 临时加材料未复核：{rec['result']} / {rec['issue_code']} / {rec['detail']}")
    _approve(conn, a2["id"])
    _, rec = services.gate_check(
        conn, appointment_id=a2["id"], gate_building_id=bid["2号楼"],
        weighbridge=WeighbridgeAdapter("exact"), vehicle_adapter=vehicles)
    print(f"    重新核验后门岗：{rec['result']} / {rec['detail']}")

    print("-" * 64)
    diffs = services.list_signoff_diffs(conn)
    print(f"签收差异台账共 {len(diffs)} 条：" +
          "；".join(f"{d['material_name']}={d['diff_code']}" for d in diffs))
    print(f"数据库：{config.DB_PATH}")
    print(f"保护照片目录：{config.PHOTO_DIR}（{len(list(config.PHOTO_DIR.glob('*')))} 张）")
    print("=" * 64)
    conn.close()

    if serve_after:
        from .ui import serve
        serve()
    return True


if __name__ == "__main__":
    run_demo()
