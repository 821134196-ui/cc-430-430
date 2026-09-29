"""演示数据：楼栋、电梯、装卸区、装修单元、保护点位、材料清单。"""
from datetime import datetime

from . import services
from .db import init_db

PROTECTION_POINTS = ["轿厢地面保护板", "轿厢内壁护板", "门套/厅门护角", "电梯厅地面铺垫"]


def seed(conn=None, now=None):
    own = conn is None
    if own:
        conn = init_db()
    now = now or datetime.now()
    if conn.execute("SELECT COUNT(*) c FROM building").fetchone()["c"]:
        return conn

    buildings = [
        ("1号楼", "绿森花园 1号楼（演示地址：云杉路 88 弄）", 2),
        ("2号楼", "绿森花园 2号楼（演示地址：云杉路 88 弄）", 1),
    ]
    building_ids = {}
    for name, addr, docks in buildings:
        cur = conn.execute(
            "INSERT INTO building(name,address,dock_count) VALUES(?,?,?)", (name, addr, docks))
        bid = cur.lastrowid
        building_ids[name] = bid
        for code, load in (("1#梯", 1000.0), ("2#梯", 800.0)):
            conn.execute(
                "INSERT INTO elevator(building_id,code,max_load_kg) VALUES(?,?,?)",
                (bid, code, load))
        for i in range(1, docks + 1):
            conn.execute(
                "INSERT INTO loading_dock(building_id,code) VALUES(?,?)",
                (bid, f"{i}#装卸区"))
        for p in PROTECTION_POINTS:
            conn.execute(
                "INSERT INTO protection_point(building_id,elevator_id,name,required) VALUES(?,?,?,1)",
                (bid, None, p))

    units = [
        ("1号楼", "1-1203", "周明远", "138****0011", [
            {"name": "实木地板", "qty": 40, "unit": "箱", "weight_kg": 480,
             "length_m": 1.2, "width_m": 0.2, "height_m": 0.08},
            {"name": "轻钢龙骨", "qty": 60, "unit": "根", "weight_kg": 180,
             "length_m": 3.0, "width_m": 0.05, "height_m": 0.03},
            {"name": "瓷砖胶", "qty": 20, "unit": "袋", "weight_kg": 400,
             "length_m": 0.6, "width_m": 0.4, "height_m": 0.15},
        ]),
        ("1号楼", "1-0801", "林晚晴", "139****0022", [
            {"name": "乳胶漆", "qty": 12, "unit": "桶", "weight_kg": 240,
             "length_m": 0.3, "width_m": 0.3, "height_m": 0.4},
            {"name": "石膏板", "qty": 30, "unit": "张", "weight_kg": 330,
             "length_m": 2.4, "width_m": 1.2, "height_m": 0.01},
        ]),
        ("2号楼", "2-0502", "何立诚", "137****0033", [
            {"name": "大理石台面", "qty": 3, "unit": "块", "weight_kg": 150,
             "length_m": 2.6, "width_m": 0.8, "height_m": 0.04},
            {"name": "橱柜板材", "qty": 25, "unit": "张", "weight_kg": 500,
             "length_m": 2.44, "width_m": 1.22, "height_m": 0.018},
        ]),
    ]
    list_ids = {}
    for bname, room, resident, phone, items in units:
        bid = building_ids[bname]
        cur = conn.execute(
            "INSERT INTO unit(building_id,room_no,resident,phone) VALUES(?,?,?,?)",
            (bid, room, resident, phone))
        uid = cur.lastrowid
        lid = services.create_material_list(conn, uid, items, now)
        list_ids[room] = lid

    if own:
        conn.close()
    return list_ids
