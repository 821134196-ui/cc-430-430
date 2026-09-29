"""命令行入口。

单条本地命令：
    ./run.sh
等价于：
    python3 -m app.cli up
会完成 建库+演示数据+样例（预约→保护确认→门岗放行→签收，含一条签收差异）并启动 Web。
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import adapters, config, services
from .db import init_db
from .seed import seed


def _ids(conn):
    b1 = conn.execute("SELECT id FROM building WHERE name='1号楼'").fetchone()["id"]
    b2 = conn.execute("SELECT id FROM building WHERE name='2号楼'").fetchone()["id"]
    e1 = conn.execute("SELECT id FROM elevator WHERE building_id=? AND code='1#梯'",
                      (b1,)).fetchone()["id"]
    e2 = conn.execute("SELECT id FROM elevator WHERE building_id=? AND code='1#梯'",
                      (b2,)).fetchone()["id"]
    d1 = conn.execute("SELECT id FROM loading_dock WHERE building_id=? LIMIT 1",
                      (b1,)).fetchone()["id"]
    d2 = conn.execute("SELECT id FROM loading_dock WHERE building_id=? LIMIT 1",
                      (b2,)).fetchone()["id"]
    u1 = conn.execute("SELECT id FROM unit WHERE room_no='1-0801'").fetchone()["id"]
    u2 = conn.execute("SELECT id FROM unit WHERE room_no='2-0502'").fetchone()["id"]
    return b1, b2, e1, e2, d1, d2, u1, u2


def run_demo(conn, now=None):
    """创建并走通两条样例：A 正常签收一致；B 签收数量有差异。"""
    now = now or datetime.now()
    # 选取一个“当前可放行”的时段（演示时间落在时段开始后 5 分钟），避免迟到判定
    hour = now.hour
    slot = next((s for s in services.SLOTS if int(s[0][:2]) <= hour < int(s[1][:2])),
                services.SLOTS[1])
    sim_now = datetime.fromisoformat(f"{now.strftime('%Y-%m-%d')}T{slot[0]}") + timedelta(minutes=5)
    date = sim_now.strftime("%Y-%m-%d")
    b1, b2, e1, e2, d1, d2, u1, u2 = _ids(conn)
    l1 = services.unit_active_list(conn, u1)
    l2 = services.unit_active_list(conn, u2)
    out = []

    existing = conn.execute(
        "SELECT id FROM appointment WHERE code LIKE 'DEMO-A-%' AND slot_date=?",
        (date,)).fetchone()
    if not existing:
        a = services.create_appointment(
            conn, b1, e1, d1, u1, "沪A·12345", "王师傅", date, slot[0], l1, sim_now)
        conn.execute("UPDATE appointment SET code=? WHERE id=?",
                     (f"DEMO-A-{date}", a["id"]))
        out.append(f"样例A 预约创建：{a['code']}")
        points = {p["id"]: {"ok": True} for p in services.protection_points(conn, b1)}
        services.confirm_protection(conn, a["id"], points, "物业张主管", sim_now)
        out.append("样例A 保护方案已确认（4 个点位照片已保存到本地目录）")
        res = services.gate_pass(conn, "沪A·12345", b1, "门岗老刘", sim_now)
        assert res["passed"], res["reason"]
        out.append(f"样例A 门岗放行：实称货物 {res['weighed_kg']:.0f}kg")
        items = services.list_detail(conn, l1)["items"]
        actual = [{"name": i["name"], "qty": i["qty"], "unit": i["unit"]} for i in items]
        so = services.signoff(conn, a["id"], "林晚晴", actual, sim_now)
        out.append("样例A 签收完成，清单与实收一致")
        a_id = a["id"]
    else:
        a_id = existing["id"]
        out.append("样例A 今日已存在，跳过（幂等）")

    existing_b = conn.execute(
        "SELECT id FROM appointment WHERE code LIKE 'DEMO-B-%' AND slot_date=?",
        (date,)).fetchone()
    if not existing_b:
        b = services.create_appointment(
            conn, b2, e2, d2, u2, "沪B·66888", "李建国", date, slot[0], l2, sim_now)
        conn.execute("UPDATE appointment SET code=? WHERE id=?",
                     (f"DEMO-B-{date}", b["id"]))
        points = {p["id"]: {"ok": True} for p in services.protection_points(conn, b2)}
        services.confirm_protection(conn, b["id"], points, "物业张主管", sim_now)
        res = services.gate_pass(conn, "沪B·66888", b2, "门岗老刘", sim_now)
        assert res["passed"], res["reason"]
        items = services.list_detail(conn, l2)["items"]
        # 第一项少收 1 件，制造签收差异
        actual = [{"name": i["name"], "qty": i["qty"] - (1 if idx == 0 else 0),
                   "unit": i["unit"]} for idx, i in enumerate(items)]
        services.signoff(conn, b["id"], "何立诚", actual, sim_now)
        out.append(f"样例B 预约+保护+放行+签收完成：{b['plate']}（含 1 条签收差异，可在签收差异页查看）")
    else:
        out.append("样例B 今日已存在，跳过（幂等）")

    out.append(f"演示时段：{date} {slot[0]}-{slot[1]}（可在楼栋日历选择该日期查看）")
    return "\n".join(out)


def serve(host, port):
    from .server import make_server
    httpd = make_server(host, port)
    print(f"服务已启动：http://{host}:{port}  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def main(argv=None):
    ap = argparse.ArgumentParser(description="装修材料进场预约与电梯保护确认系统")
    sub = ap.add_subparsers(dest="cmd")
    p_up = sub.add_parser("up", help="建库+演示数据+样例工作流，然后启动 Web")
    p_up.add_argument("--host", default="127.0.0.1")
    p_up.add_argument("--port", type=int, default=8000)
    p_demo = sub.add_parser("demo", help="只创建样例工作流，不启动服务")
    p_serve = sub.add_parser("serve", help="只启动 Web 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("reset", help="删除本地数据库与照片（演示数据）")
    args = ap.parse_args(argv)

    if args.cmd == "reset":
        for p in (config.DB_PATH,):
            if p.exists():
                p.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(config.DB_PATH) + suffix)
            if p.exists():
                p.unlink()
        for f in config.PHOTO_DIR.glob("*.png"):
            f.unlink()
        print("已重置本地数据")
        return
    if args.cmd in (None, "up", "demo"):
        conn = init_db()
        seed(conn)
        print(run_demo(conn))
        conn.close()
        if args.cmd == "demo":
            return
    if args.cmd in (None, "up", "serve"):
        serve(args.host if hasattr(args, "host") else "127.0.0.1",
              args.port if hasattr(args, "port") else 8000)


if __name__ == "__main__":
    sys.exit(main())
