"""测试：容量竞争、占位过期、重量边界、字段脱敏、签收幂等，
以及临时增改材料重核、错楼栋、电梯故障、保护板缺失、迟到、并发只放行一车。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import adapters, config, services          # noqa: E402
from app.db import connect, init_db                 # noqa: E402
from app.seed import seed                           # noqa: E402

NOW = datetime(2026, 9, 29, 8, 5)   # 固定“当前时间”，08:00 时段开始后 5 分钟（宽限内）
DATE = "2026-09-29"
SLOT = "08:00"


class Base(unittest.TestCase):
    """每个用例一个独立临时库（文件库，供多线程用例共享）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "t.db"
        config.PHOTO_DIR = Path(self.tmp.name) / "photos"
        config.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = init_db(self.db_path)
        seed(self.conn, now=NOW)
        self.b1 = self._id("SELECT id FROM building WHERE name='1号楼'")
        self.b2 = self._id("SELECT id FROM building WHERE name='2号楼'")
        self.e1 = self._id("SELECT id FROM elevator WHERE building_id=? AND code='1#梯'", self.b1)
        self.d1 = self._id("SELECT id FROM loading_dock WHERE building_id=? LIMIT 1", self.b1)
        self.u1 = self._id("SELECT id FROM unit WHERE room_no='1-0801'")   # 清单 570kg
        self.u2 = self._id("SELECT id FROM unit WHERE room_no='2-0502'")   # 清单 650kg
        self.l1 = services.unit_active_list(self.conn, self.u1)
        self.l2 = services.unit_active_list(self.conn, self.u2)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _id(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()["id"]

    def book(self, plate="沪A·12345", unit=None, lid=None, building=None,
             elevator=None, dock=None, now=NOW):
        return services.create_appointment(
            self.conn, building or self.b1, elevator or self.e1, dock or self.d1,
            unit or self.u1, plate, "测试司机", DATE, SLOT, lid or self.l1, now)

    def protect(self, aid, by="物业张"):
        pts = {p["id"]: {"ok": True} for p in services.protection_points(self.conn, self.b1)}
        services.confirm_protection(self.conn, aid, pts, by, NOW)

    def book_protected(self, plate="沪A·12345", **kw):
        a = self.book(plate=plate, **kw)
        self.protect(a["id"])
        return a


class TestCapacityRace(Base):
    """容量竞争：同一时段并发确认，只允许一辆车成功。"""

    def test_concurrent_booking_only_one_wins(self):
        errors, wins = [], []

        def worker(plate):
            c = connect(self.db_path)
            try:
                services.create_appointment(c, self.b1, self.e1, self.d1, self.u1,
                                            plate, "司机", DATE, SLOT, self.l1, NOW)
                wins.append(plate)
            except services.CapacityError:
                errors.append(plate)
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(f"沪T·{i:05d}",))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(wins), 1, f"电梯同时段容量为1，应只有1辆车成功，实际 {wins}")
        self.assertEqual(len(errors), 7)
        used = self.conn.execute(
            "SELECT used FROM slot_occupancy WHERE resource=? AND slot_date=? AND slot_start=?",
            (f"elev:{self.e1}", DATE, SLOT)).fetchone()["used"]
        self.assertEqual(used, 1)

    def test_second_booking_same_slot_rejected(self):
        self.book(plate="沪A·12345")
        with self.assertRaises(services.CapacityError):
            self.book(plate="沪B·66888")

    def test_building_dock_capacity(self):
        """1号楼 2 个装卸车位：同楼栋同时段第 3 辆车（另一电梯/车位）被拒。"""
        e2 = self._id("SELECT id FROM elevator WHERE building_id=? AND code='2#梯'", self.b1)
        d2 = self._id("SELECT id FROM loading_dock WHERE building_id=? AND code='2#装卸区'", self.b1)
        self.book(plate="沪A·12345")
        # 2#梯限重 800，1-0801 清单 570kg 可过
        self.book(plate="沪B·66888", elevator=e2, dock=d2)
        with self.assertRaises(services.CapacityError):
            self.book(plate="沪C·90210", elevator=e2, dock=d2)

    def test_concurrent_gate_pass_only_one(self):
        """同一预约并发门岗确认，只放行一次。"""
        a = self.book_protected()
        results = []

        def worker():
            c = connect(self.db_path)
            try:
                r = services.gate_pass(c, a["plate"], self.b1, "门岗", NOW)
                results.append(r["passed"])
            except services.DomainError:
                results.append(False)
            finally:
                c.close()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for x in results if x), 1)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM gate_pass WHERE appointment_id=?", (a["id"],)).fetchone()["c"]
        self.assertEqual(n, 1)


class TestHoldExpiry(Base):
    """占位过期：held 超过保留时限自动释放占用。"""

    def test_expired_hold_released(self):
        a = self.book()
        future = NOW + timedelta(minutes=config.HOLD_MINUTES + 1)
        n = services.release_expired(self.conn, future)
        self.assertEqual(n, 1)
        st = self.conn.execute("SELECT status FROM appointment WHERE id=?",
                               (a["id"],)).fetchone()["status"]
        self.assertEqual(st, config.ST_EXPIRED)
        used = self.conn.execute(
            "SELECT used FROM slot_occupancy WHERE resource=? AND slot_date=? AND slot_start=?",
            (f"elev:{self.e1}", DATE, SLOT)).fetchone()["used"]
        self.assertEqual(used, 0, "过期后名额必须释放")
        # 释放后其他车辆可再约同一时段
        self.book(plate="沪B·66888")

    def test_fresh_hold_not_released(self):
        self.book()
        n = services.release_expired(self.conn, NOW + timedelta(minutes=config.HOLD_MINUTES - 1))
        self.assertEqual(n, 0)

    def test_protected_expires_after_slot_end(self):
        a = self.book_protected()
        late = datetime.fromisoformat(f"{DATE}T10:01")   # 时段 08:00-10:00 已结束
        n = services.release_expired(self.conn, late)
        self.assertEqual(n, 1)
        st = self.conn.execute("SELECT status FROM appointment WHERE id=?",
                               (a["id"],)).fetchone()["status"]
        self.assertEqual(st, config.ST_EXPIRED)


class TestWeightBoundary(Base):
    """重量边界：申报重量与电梯载重、地磅实称的边界判定。"""

    def test_declared_weight_at_limit_ok(self):
        lid = services.create_material_list(self.conn, self.u1, [
            {"name": "边界材料", "qty": 1, "unit": "批", "weight_kg": 1000.0}], NOW)
        a = self.book(lid=lid)   # 1#梯限重 1000，恰好等于上限应允许
        self.assertEqual(a["status"], config.ST_HELD)

    def test_declared_weight_over_limit_rejected(self):
        lid = services.create_material_list(self.conn, self.u1, [
            {"name": "超重材料", "qty": 1, "unit": "批", "weight_kg": 1000.01}], NOW)
        with self.assertRaises(services.DomainError):
            self.book(lid=lid)

    def test_weighed_over_limit_denied_and_logged(self):
        a = self.book_protected()
        wb = adapters.WeighbridgeAdapter(overrides={"沪A·12345": 1200.0})
        res = services.gate_pass(self.conn, a["plate"], self.b1, "门岗", NOW, weighbridge=wb)
        self.assertFalse(res["passed"])
        self.assertIn(config.EX_OVERWEIGHT, res["exceptions"])
        kinds = [e["kind"] for e in services.exceptions(self.conn, a["id"])]
        self.assertIn(config.EX_OVERWEIGHT, kinds)

    def test_weighed_at_limit_passes(self):
        a = self.book_protected()
        wb = adapters.WeighbridgeAdapter(overrides={"沪A·12345": 1000.0})
        res = services.gate_pass(self.conn, a["plate"], self.b1, "门岗", NOW, weighbridge=wb)
        self.assertTrue(res["passed"])

    def test_overlong_flag(self):
        lid = services.create_material_list(self.conn, self.u1, [
            {"name": "长龙骨", "qty": 1, "unit": "根", "weight_kg": 10,
             "length_m": config.OVERLONG_M}], NOW)
        d = services.list_detail(self.conn, lid)
        self.assertEqual(d["list"]["has_overlong"], 1)
        self.assertEqual(d["items"][0]["overlong"], 1)
        lid2 = services.create_material_list(self.conn, self.u1, [
            {"name": "短板", "qty": 1, "unit": "张", "weight_kg": 10,
             "length_m": config.OVERLONG_M - 0.01}], NOW)
        self.assertEqual(services.list_detail(self.conn, lid2)["list"]["has_overlong"], 0)


class TestGateMasking(Base):
    """字段脱敏：门岗视图只含放行必要字段，不含住户隐私。"""

    def test_gate_view_minimal_fields(self):
        a = self.book_protected()
        v = services.gate_view(self.conn, a["id"])
        self.assertEqual(set(v.keys()), {
            "code", "plate", "driver", "building", "elevator",
            "slot", "status", "list_weight", "list_overlong"})
        forbidden = ("resident", "phone", "room", "room_no", "unit", "address")
        for k in v:
            self.assertNotIn(k, forbidden)
        blob = str(v)
        self.assertNotIn("林晚晴", blob)
        self.assertNotIn("139", blob)
        self.assertNotIn("0801", blob)


class TestSignoffIdempotent(Base):
    """签收幂等与终态保护。"""

    def _passed(self):
        a = self.book_protected()
        res = services.gate_pass(self.conn, a["plate"], self.b1, "门岗", NOW)
        assert res["passed"]
        return a

    def test_signoff_idempotent(self):
        a = self._passed()
        items = services.list_detail(self.conn, self.l1)["items"]
        actual = [{"name": i["name"], "qty": i["qty"], "unit": i["unit"]} for i in items]
        r1 = services.signoff(self.conn, a["id"], "住户", actual, NOW)
        self.assertFalse(r1["idempotent"])
        tampered = [{"name": i["name"], "qty": 999, "unit": i["unit"]} for i in items]
        r2 = services.signoff(self.conn, a["id"], "住户", tampered, NOW)
        self.assertTrue(r2["idempotent"], "重复签收必须返回首次记录")
        self.assertEqual(r1["signoff"]["id"], r2["signoff"]["id"])
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM signoff WHERE appointment_id=?", (a["id"],)).fetchone()["c"]
        self.assertEqual(n, 1)
        # 首次一致，重复提交篡改数量不应产生差异异常
        kinds = [e["kind"] for e in services.exceptions(self.conn, a["id"])]
        self.assertNotIn(config.EX_SIGNOFF_DIFF, kinds)

    def test_signoff_diff_logged(self):
        a = self._passed()
        items = services.list_detail(self.conn, self.l1)["items"]
        actual = [{"name": i["name"], "qty": i["qty"] - 1, "unit": i["unit"]} for i in items]
        r = services.signoff(self.conn, a["id"], "住户", actual, NOW)
        self.assertEqual(r["signoff"]["matched"], 0)
        kinds = [e["kind"] for e in services.exceptions(self.conn, a["id"])]
        self.assertIn(config.EX_SIGNOFF_DIFF, kinds)

    def test_signed_cannot_rollback(self):
        a = self._passed()
        items = services.list_detail(self.conn, self.l1)["items"]
        actual = [{"name": i["name"], "qty": i["qty"], "unit": i["unit"]} for i in items]
        services.signoff(self.conn, a["id"], "住户", actual, NOW)
        with self.assertRaises(services.DomainError):
            services._set_status(self.conn, a["id"], config.ST_HELD, NOW)
        with self.assertRaises(services.DomainError):
            services._set_status(self.conn, a["id"], config.ST_PASSED, NOW)

    def test_signoff_requires_passed(self):
        a = self.book_protected()
        with self.assertRaises(services.DomainError):
            services.signoff(self.conn, a["id"], "住户", [], NOW)


class TestMaterialRecheck(Base):
    """临时增加材料必须重新核验。"""

    def test_add_material_blocks_protection_confirm(self):
        a = self.book()
        services.add_material_item(self.conn, self.l1,
                                   {"name": "临时水泥", "qty": 5, "unit": "袋",
                                    "weight_kg": 100}, NOW)
        pts = {p["id"]: {"ok": True} for p in services.protection_points(self.conn, self.b1)}
        with self.assertRaises(services.DomainError):
            services.confirm_protection(self.conn, a["id"], pts, "物业", NOW)
        # 重新核验后恢复可确认
        services.verify_material_list(self.conn, self.l1, NOW)
        services.confirm_protection(self.conn, a["id"], pts, "物业", NOW)
        st = self.conn.execute("SELECT status FROM appointment WHERE id=?",
                               (a["id"],)).fetchone()["status"]
        self.assertEqual(st, config.ST_PROTECTED)

    def test_unverified_list_cannot_book(self):
        lid = services.create_material_list(self.conn, self.u1, [
            {"name": "x", "qty": 1, "unit": "件", "weight_kg": 10}], NOW)
        services.add_material_item(self.conn, lid,
                                   {"name": "y", "qty": 1, "unit": "件", "weight_kg": 10}, NOW)
        with self.assertRaises(services.DomainError):
            self.book(lid=lid)


class TestGateRules(Base):
    """错楼栋 / 电梯故障 / 保护板缺失 / 迟到。"""

    def test_wrong_building(self):
        a = self.book_protected()
        res = services.gate_pass(self.conn, a["plate"], self.b2, "门岗", NOW)
        self.assertFalse(res["passed"])
        self.assertIn(config.EX_WRONG_BUILDING, res["exceptions"])

    def test_elevator_fault(self):
        a = self.book_protected()
        self.conn.execute("UPDATE elevator SET status='fault' WHERE id=?", (self.e1,))
        res = services.gate_pass(self.conn, a["plate"], self.b1, "门岗", NOW)
        self.assertFalse(res["passed"])
        self.assertIn(config.EX_ELEVATOR_FAULT, res["exceptions"])

    def test_fault_elevator_cannot_book(self):
        self.conn.execute("UPDATE elevator SET status='fault' WHERE id=?", (self.e1,))
        with self.assertRaises(services.DomainError):
            self.book()

    def test_missing_protection_board(self):
        a = self.book()
        pts = services.protection_points(self.conn, self.b1)
        checks = {p["id"]: {"ok": True} for p in pts[1:]}   # 第一个必检点缺失
        with self.assertRaises(services.DomainError):
            services.confirm_protection(self.conn, a["id"], checks, "物业", NOW)

    def test_late_arrival_denied(self):
        a = self.book_protected()
        late = datetime.fromisoformat(f"{DATE}T{SLOT}") + timedelta(
            minutes=config.LATE_GRACE_MINUTES + 1)
        res = services.gate_pass(self.conn, a["plate"], self.b1, "门岗", late)
        self.assertFalse(res["passed"])
        self.assertIn(config.EX_LATE, res["exceptions"])

    def test_unprotected_cannot_pass(self):
        a = self.book()
        res = services.gate_pass(self.conn, a["plate"], self.b1, "门岗", NOW)
        self.assertFalse(res["passed"])
        self.assertIn("保护方案", res["reason"])

    def test_no_appointment(self):
        res = services.gate_pass(self.conn, "沪Z·00000", self.b1, "门岗", NOW)
        self.assertFalse(res["passed"])
        self.assertEqual(res["reason"], "无有效预约")


if __name__ == "__main__":
    unittest.main()
