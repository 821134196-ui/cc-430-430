"""演示数据：地址、住户、车牌均为虚构样例。"""
from .db import init_db, connect
from . import services
from .adapters import VehicleAdapter, WeighbridgeAdapter

BUILDINGS = [
    # name, address, loading_bays
    ("1号楼", "绿谷森邻小区 云杉路 88 弄 1 号楼", 2),
    ("2号楼", "绿谷森邻小区 云杉路 88 弄 2 号楼", 1),
    ("3号楼", "绿谷森邻小区 云杉路 88 弄 3 号楼", 2),
]

# building_name -> [电梯 code, 载重kg, 轿厢, 门洞]
ELEVATORS = {
    "1号楼": [
        ("1#客梯", 1000, "160x150x230", "90x210"),
        ("1#货梯", 1600, "200x180x240", "120x220"),
    ],
    "2号楼": [
        ("2#客梯", 1000, "160x150x230", "90x210"),
        ("2#货梯", 1350, "180x160x235", "110x215"),
    ],
    "3号楼": [
        ("3#货梯", 1600, "200x180x240", "120x220"),
    ],
}

UNITS = [
    # building, room, resident, phone
    ("1号楼", "1-1202", "演示住户·林一", "139-0000-1202"),
    ("1号楼", "1-0803", "演示住户·吴二", "139-0000-0803"),
    ("2号楼", "2-0501", "演示住户·沈三", "139-0000-0501"),
    ("3号楼", "3-1601", "演示住户·韩四", "139-0000-1601"),
]

PROTECTION_POINTS = {
    "1号楼": [
        ("一层大堂地面", "floor"), ("一层大堂阳角", "corner"),
        ("电梯轿厢壁板", "wall"), ("电梯门洞阳角", "corner"),
    ],
    "2号楼": [
        ("一层大堂地面", "floor"), ("电梯轿厢壁板", "wall"),
    ],
    "3号楼": [
        ("一层大堂地面", "floor"), ("卸货通道墙面", "wall"),
        ("电梯轿厢壁板", "wall"), ("电梯门洞阳角", "corner"),
    ],
}

# unit room -> materials (name, weight, l, w, h, overlong)
MATERIALS = {
    "1-1202": [
        ("强化木地板 24㎡", 280, 122, 20, 1.2, 0),
        ("轻钢龙骨 40 根", 160, 300, 5, 3, 1),   # 超长
        ("白乳胶 6 桶", 90, 30, 30, 35, 0),
    ],
    "1-0803": [
        ("瓷砖 40 箱", 800, 80, 80, 60, 0),
        ("水泥 20 袋", 1000, 60, 40, 10, 0),
    ],
    "2-0501": [
        ("成品衣柜板材包", 320, 240, 60, 8, 1),
        ("乳胶漆 8 桶", 120, 30, 30, 35, 0),
    ],
    "3-1601": [
        ("大理石台面", 220, 260, 70, 3, 1),
        ("袋装粘结剂 15 袋", 375, 50, 40, 12, 0),
    ],
}


def seed(conn=None, verbose=True):
    own = conn is None
    if own:
        conn = connect()
    init_db(conn)
    # 幂等：已有楼栋则跳过
    if conn.execute("SELECT COUNT(*) c FROM building").fetchone()["c"]:
        if verbose:
            print("演示数据已存在，跳过。")
        if own:
            conn.close()
        return

    def log(*a):
        if verbose:
            print(*a)

    for name, addr, bays in BUILDINGS:
        cur = conn.execute(
            "INSERT INTO building(name, address, loading_bays) VALUES (?,?,?)",
            (name, addr, bays))
        bid = cur.lastrowid
        for code, load, cabin, door in ELEVATORS[name]:
            conn.execute(
                """INSERT INTO elevator(building_id, code, load_kg, cabin_cm, door_cm)
                   VALUES (?,?,?,?,?)""", (bid, code, load, cabin, door))
        for pname, kind in PROTECTION_POINTS[name]:
            conn.execute(
                "INSERT INTO protection_point(building_id, name, kind) VALUES (?,?,?)",
                (bid, pname, kind))

    room_to_unit = {}
    for bname, room, resident, phone in UNITS:
        bid = conn.execute("SELECT id FROM building WHERE name=?", (bname,)).fetchone()["id"]
        cur = conn.execute(
            "INSERT INTO unit(building_id, room_no, resident, phone) VALUES (?,?,?,?)",
            (bid, room, resident, phone))
        room_to_unit[room] = cur.lastrowid
        for mname, w, l, wid, h, over in MATERIALS[room]:
            conn.execute(
                """INSERT INTO material(unit_id, name, weight_kg, length_cm,
                   width_cm, height_cm, overlong) VALUES (?,?,?,?,?,?,?)""",
                (cur.lastrowid, mname, w, l, wid, h, over))
        log(f"  已建单元 {bname} {room}（{resident}）材料 {len(MATERIALS[room])} 项")

    log("演示数据写入完成。")
    if own:
        conn.close()


def adapters(weigh_mode="exact", **kw):
    return VehicleAdapter(), WeighbridgeAdapter(mode=weigh_mode, **kw)
