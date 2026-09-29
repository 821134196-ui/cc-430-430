"""测试：容量竞争、占位过期、重量边界、字段脱敏、签收幂等。

另覆盖迟到 / 错楼栋 / 电梯故障 / 保护板缺失 / 临时加材料复核等门岗登记。
运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, services
from app.adapters import VehicleAdapter, WeighbridgeAdapter
from app.db import connect, init_db
from app.seed import seed

TODAY = datetime.now().strftime("%Y-%m-%d")
PLATE_OK = "沪D·Q1046"  # 备案可进入全部楼栋


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        config.DB_PATH = __import__("pathlib").Path(d) / "booking.db"
        config.PHOTO_DIR = __import__("pathlib").Path(d) / "photos"
        config.DATA_DIR = __import__("pathlib").Path(d)
        config.HOLD_TTL_SECONDS = 900
        self.conn = connect()
        init_db(self.conn)
        seed(self.conn, verbose=False)
        self.vehicles = VehicleAdapter()
        # id 速查
        self.bid = {r["name"]: r["id"]
                    for r in self.conn.execute("SELECT id,name FROM building")}
        self.eid = {}
        for r in self.conn.execute("SELECT e.id, b.name bn, e.code FROM elevator e "
                                   "JOIN building b ON b.id=e.building_id"):
            self.eid[(r["bn"], r["code"])] = r["id"]
        self.uid = {r["room_no"]: r["id"]
                    for r in self.conn.execute("SELECT id,room_no FROM unit")}

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def book(self, room, elev_key, start, plate=PLATE_OK, date=TODAY, **kw):
        return services.create_appointment(
            self.conn, unit_id=self.uid[room], elevator_id=self.eid[elev_key],
            slot_date=date, slot_start=start, plate=plate,
            vehicle_adapter=self.vehicles, **kw)

    def approve(self, appt_id):
        pts = self.conn.execute(
            "SELECT id FROM protection_point WHERE building_id="
            "(SELECT building_id FROM appointment WHERE id=?) AND required=1",
            (appt_id,)).fetchall()
        services.record_protection_checks(
            self.conn, appt_id, [(p["id"], 1, None) for p in pts], "物业·测试")
        return services.confirm_protection(self.conn, appt_id, "物业·测试")

    def gate(self, appt_id, building=None, wb=None, now=None, vehicles=None):
        return services.gate_check(
            self.conn, appointment_id=appt_id,
            gate_building_id=building if building is not None else
            services.get_appointment(self.conn, appt_id)["building_id"],
            weighbridge=wb or WeighbridgeAdapter("exact"),
            vehicle_adapter=vehicles or self.vehicles, now=now)

    def epoch(self, hhmm, date=TODAY):
        return datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H:%M").timestamp()


class TestCapacity(AppTestBase):
    def test_same_elevator_slot_only_one_succeeds(self):
        """同电梯同时段：10 个并发线程，仅 1 条占位成功。"""
        results, errors = [], []
        lock = threading.Lock()

        def worker():
            try:
                c = connect()
                a = services.create_appointment(
                    c, unit_id=self.uid["1-1202"],
                    elevator_id=self.eid[("1号楼", "1#货梯")],
                    slot_date=TODAY, slot_start="11:00", plate=PLATE_OK,
                    vehicle_adapter=VehicleAdapter())
                with lock:
                    results.append(a["id"])
                c.close()
            except services.BusinessError:
                with lock:
                    errors.append(True)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1, f"应只成功 1 条，实际 {results}")
        self.assertEqual(len(errors), 9)
        occ = self.conn.execute(
            "SELECT COUNT(*) c FROM appointment WHERE elevator_id=? AND slot_date=? "
            "AND slot_start='11:00' AND status IN ('held','confirmed')",
            (self.eid[("1号楼", "1#货梯")], TODAY)).fetchone()["c"]
        self.assertEqual(occ, 1)

    def test_loading_bay_capacity(self):
        """装卸区容量：2 号楼仅 1 个车位，第二部梯同时段被拒。"""
        a = self.book("2-0501", ("2号楼", "2#客梯"), "09:30")
        self.assertEqual(a["status"], "held")
        with self.assertRaises(services.CapacityError) as cm:
            self.book("2-0501", ("2号楼", "2#货梯"), "09:30", plate="沪B·K7751")
        self.assertEqual(cm.exception.issue_code, "bay_full")

    def test_bay_ticket_freed_after_release(self):
        """释放后车位与梯位都回收，可重新预约。"""
        self.book("2-0501", ("2号楼", "2#客梯"), "15:30")
        services.expire_holds(self.conn, now=self.epoch("18:00") + 10)
        again = self.book("2-0501", ("2号楼", "2#客梯"), "15:30", plate="沪B·K7751")
        self.assertEqual(again["status"], "held")

    def test_concurrent_gate_double_click_only_one_pass(self):
        """同一时段并发确认：多次门岗并发调用只允许一辆车成功。"""
        a = self.book("1-1202", ("1号楼", "1#货梯"), "10:00")
        self.approve(a["id"])
        outcomes = []
        lock = threading.Lock()

        def worker():
            c = connect()
            try:
                _, rec = services.gate_check(
                    c, appointment_id=a["id"],
                    gate_building_id=self.bid["1号楼"],
                    weighbridge=WeighbridgeAdapter("exact"),
                    vehicle_adapter=VehicleAdapter())
                with lock:
                    outcomes.append(rec["result"])
            finally:
                c.close()

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("pass"), 1)
        self.assertEqual(outcomes.count("reject"), 11)


class TestHoldExpiry(AppTestBase):
    def test_ttl_expiry_releases_hold(self):
        """占位 TTL 到期自动释放。"""
        a = self.book("1-1202", ("1号楼", "1#货梯"), "08:00")
        self.conn.execute("UPDATE appointment SET hold_expires_at=? WHERE id=?",
                          (str(self.epoch("08:00") - 1), a["id"]))
        released = services.expire_holds(self.conn, now=self.epoch("08:00"))
        self.assertEqual(released, [(a["id"], "hold_ttl")])
        self.assertEqual(services.get_appointment(self.conn, a["id"])["status"], "released")
        # 车位票已删除
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM bay_ticket WHERE appointment_id=?",
            (a["id"],)).fetchone()["c"], 0)

    def test_slot_passed_releases_hold(self):
        """时段结束仍 held（即便 TTL 未到）也自动释放。"""
        a = self.book("1-1202", ("1号楼", "1#货梯"), "08:00")
        self.conn.execute("UPDATE appointment SET hold_expires_at=? WHERE id=?",
                          (str(self.epoch("08:00") + 10 ** 9), a["id"]))
        released = services.expire_holds(
            self.conn, now=self.epoch("08:30") + config.LATE_GRACE_MINUTES * 60 + 1)
        self.assertEqual(released[0][1], "slot_passed")

    def test_released_slot_rebookable(self):
        a = self.book("1-1202", ("1号楼", "1#货梯"), "08:00")
        self.conn.execute("UPDATE appointment SET hold_expires_at='0' WHERE id=?", (a["id"],))
        services.expire_holds(self.conn, now=self.epoch("09:00"))
        again = self.book("1-1202", ("1号楼", "1#货梯"), "08:00", plate="沪A·D3268")
        self.assertEqual(again["status"], "held")

    def test_confirmed_not_expired_by_ttl(self):
        a = self.book("1-1202", ("1号楼", "1#货梯"), "08:00")
        self.approve(a["id"])
        services.expire_holds(self.conn, now=self.epoch("08:00") - 1)
        self.assertEqual(services.get_appointment(self.conn, a["id"])["status"],
                         "confirmed")


class TestWeightBoundary(AppTestBase):
    def _fill_to_exact_load(self, appt_id, load_kg):
        total, _, _ = services._unit_material_totals(
            self.conn, services.get_appointment(self.conn, appt_id)["unit_id"], appt_id)
        services.add_material(self.conn, appt_id, name="边界配重",
                              weight_kg=round(load_kg - total, 1),
                              length_cm=50, width_cm=50, height_cm=50)

    def test_booking_rejects_declared_overweight(self):
        """1-0803 材料 1800kg，超 1#货梯 1600kg，预约阶段拒绝。"""
        with self.assertRaises(services.BusinessError) as cm:
            self.book("1-0803", ("1号楼", "1#货梯"), "09:00")
        self.assertEqual(cm.exception.issue_code, "overweight")

    def test_exact_load_allowed_then_overweight_rejected_at_gate(self):
        """总重恰等于载重 1600kg：预约与精确称重放行；超出 0.1kg 门岗拒绝。"""
        a = self.book("1-1202", ("1号楼", "1#货梯"), "09:00")
        self._fill_to_exact_load(a["id"], 1600)
        self.assertEqual(services.get_appointment(self.conn, a["id"])["total_weight_kg"], 1600)
        self.approve(a["id"])
        _, rec = self.gate(a["id"], wb=WeighbridgeAdapter("exact"))
        self.assertEqual(rec["result"], "pass")

        # 精确到边界放行后已 gate_passed；新造一条（另一独立单元）测超重称重
        b = self.book("3-1601", ("3号楼", "3#货梯"), "09:30", plate="沪A·D3268")
        self._fill_to_exact_load(b["id"], 1600)
        self.approve(b["id"])
        _, rec = self.gate(b["id"], wb=WeighbridgeAdapter("overweight", force_extra_kg=0.1))
        self.assertEqual(rec["result"], "reject")
        self.assertEqual(rec["issue_code"], "overweight")
        self.assertIn("1600.1", rec["detail"])
        # 超重拒绝不改状态，仍可复测
        self.assertEqual(services.get_appointment(self.conn, b["id"])["status"], "confirmed")

    def test_add_material_past_load_rejected(self):
        a = self.book("1-1202", ("1号楼", "1#货梯"), "09:00")
        with self.assertRaises(services.BusinessError) as cm:
            services.add_material(self.conn, a["id"], name="超重件",
                                  weight_kg=5000, length_cm=10, width_cm=10, height_cm=10)
        self.assertEqual(cm.exception.issue_code, "overweight")


class TestMasking(AppTestBase):
    def test_gate_view_whitelist(self):
        """门岗视图只含必要字段：无住户/司机电话、姓名、地址。"""
        a = self.book("1-1202", ("1号楼", "1#货梯"), "09:00")
        view = services.gate_view(self.conn, a)
        self.assertEqual(set(view), set(services.GATE_FIELDS))
        for secret in ("driver_phone", "resident", "phone", "address", "note"):
            self.assertNotIn(secret, view)
        # 必要字段存在且正确
        self.assertEqual(view["vehicle_plate"], PLATE_OK)
        self.assertEqual(view["building_name"], "1号楼")
        self.assertEqual(view["room_no"], "1-1202")
        self.assertEqual(view["load_kg"], 1600)

    def test_gate_html_contains_no_phone(self):
        """门岗渲染页面不泄露电话字段。"""
        from app import ui
        a = self.book("1-1202", ("1号楼", "1#货梯"), "09:00")
        self.approve(a["id"])
        html_text = ui.page_gate_lookup(self.conn, {
            "plate": [PLATE_OK], "building": [str(self.bid["1号楼"])], "wb": ["exact"]})
        self.assertNotIn("13800004004", html_text)   # 司机电话
        self.assertNotIn("139-0000-1202", html_text)  # 住户电话
        self.assertNotIn("演示住户·林一", html_text)   # 住户姓名
        self.assertIn(PLATE_OK, html_text)

    def test_raw_record_still_keeps_phone_for_back_office(self):
        """后台预约详情仍可查司机电话（仅门岗端脱敏）。"""
        a = self.book("1-1202", ("1号楼", "1#货梯"), "09:00")
        self.assertTrue(a["driver_phone"])


class TestSignoff(AppTestBase):
    def _passed_appointment(self, start="09:00", plate=PLATE_OK):
        a = self.book("1-1202", ("1号楼", "1#货梯"), start, plate=plate)
        self.approve(a["id"])
        self.gate(a["id"])
        return a

    def test_signoff_idempotent(self):
        a = self._passed_appointment()
        _, _, mats = services._unit_material_totals(self.conn, a["unit_id"], a["id"])
        items = [(m["id"], 1, m["weight_kg"]) for m in mats]
        first = services.signoff(self.conn, a["id"], "演示住户·林一", items)
        self.assertFalse(first["idempotent"])
        second = services.signoff(self.conn, a["id"], "演示住户·林一", items)
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["signoff"]["id"], second["signoff"]["id"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM signoff WHERE appointment_id=?",
            (a["id"],)).fetchone()["c"], 1)
        self.assertEqual(services.get_appointment(self.conn, a["id"])["status"], "signed")

    def test_signed_cannot_revert_to_arrived(self):
        """已签收材料不能回退为未到场。"""
        a = self._passed_appointment("09:30", plate="沪A·D3268")
        services.mark_arrived(self.conn, a["id"])
        _, _, mats = services._unit_material_totals(self.conn, a["unit_id"], a["id"])
        services.signoff(self.conn, a["id"], "林一",
                         [(m["id"], 1, m["weight_kg"]) for m in mats])
        with self.assertRaises(services.BusinessError) as cm:
            services.mark_arrived(self.conn, a["id"])
        self.assertEqual(cm.exception.issue_code, "signed_locked")
        self.assertEqual(services.get_appointment(self.conn, a["id"])["status"], "signed")

    def test_short_and_diff_registered(self):
        a = self._passed_appointment("10:00")
        _, _, mats = services._unit_material_totals(self.conn, a["unit_id"], a["id"])
        target = mats[0]
        items = []
        for m in mats:
            if m["id"] == target["id"]:
                items.append((m["id"], 0, 0))  # 整件短少
            else:
                items.append((m["id"], 1, m["weight_kg"]))
        payload = services.signoff(self.conn, a["id"], "林一", items)
        codes = {i["material_id"]: i["diff_code"] for i in payload["items"]}
        self.assertEqual(codes[target["id"]], "short")
        diffs = services.list_signoff_diffs(self.conn)
        self.assertTrue(any(d["vehicle_plate"] == PLATE_OK for d in diffs))


class TestGateIssues(AppTestBase):
    def test_late_rejected(self):
        a = self.book("1-1202", ("1号楼", "1#货梯"), "13:30")
        self.approve(a["id"])
        _, rec = self.gate(a["id"], now=self.epoch("14:30"))
        self.assertEqual(rec["issue_code"], "late")

    def test_wrong_building_rejected(self):
        a = self.book("1-1202", ("1号楼", "1#货梯"), "09:00")
        self.approve(a["id"])
        _, rec = self.gate(a["id"], building=self.bid["2号楼"])
        self.assertEqual(rec["issue_code"], "wrong_building")

    def test_plate_not_permitted_for_building(self):
        # 沪C·8F209 仅备案 3 号楼，却预约 1 号楼：门岗以错楼栋登记
        a = self.book("1-1202", ("1号楼", "1#货梯"), "09:30", plate="沪C·8F209")
        self.approve(a["id"])
        _, rec = self.gate(a["id"], building=self.bid["1号楼"])
        self.assertEqual(rec["issue_code"], "wrong_building")

    def test_elevator_fault_rejected(self):
        a = self.book("1-1202", ("1号楼", "1#货梯"), "10:00")
        self.approve(a["id"])
        self.conn.execute("UPDATE elevator SET status='fault' WHERE id=?",
                          (self.eid[("1号楼", "1#货梯")],))
        _, rec = self.gate(a["id"])
        self.assertEqual(rec["issue_code"], "elevator_fault")

    def test_protection_required_before_pass(self):
        a = self.book("1-1202", ("1号楼", "1#货梯"), "10:30")
        # 未做保护确认（held 占位状态）
        _, rec = self.gate(a["id"])
        self.assertEqual(rec["issue_code"], "not_confirmed")

    def test_missing_protection_board_blocks_approval_and_gate(self):
        a = self.book("1-1202", ("1号楼", "1#货梯"), "11:00")
        pts = self.conn.execute(
            "SELECT id FROM protection_point WHERE building_id=? AND required=1",
            (self.bid["1号楼"],)).fetchall()
        services.record_protection_checks(
            self.conn, a["id"], [(pts[0]["id"], 1, None)], "物业")
        with self.assertRaises(services.BusinessError):
            services.confirm_protection(self.conn, a["id"], "物业")

    def test_added_material_requires_recheck(self):
        a = self.book("1-1202", ("1号楼", "1#货梯"), "11:30")
        self.approve(a["id"])
        services.add_material(self.conn, a["id"], name="临时石膏线",
                              weight_kg=20, length_cm=200, width_cm=10,
                              height_cm=5, overlong=True)
        _, rec = self.gate(a["id"])
        self.assertEqual(rec["issue_code"], "no_protection")
        self.approve(a["id"])  # 重新核验
        _, rec = self.gate(a["id"], wb=WeighbridgeAdapter("exact"))
        self.assertEqual(rec["result"], "pass")


if __name__ == "__main__":
    unittest.main(verbosity=2)
