import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store


class BatchFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = BatchService(Store(Path(self.tmp.name) / "b.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.f2 = self.s.register_factory("qa", "qa", "F2", "二厂", "CN")["id"]
        self.future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def test_full_investigation_retest_rework_and_release(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-1", "药片", "2026-01-01", "2028-01-01")
        dev = self.s.add_deviation("operator", "operator", self.f1, batch["id"], "minor", "装量轻微偏离", self.future, batch["revision"])
        failed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 89, 95, 105, dev["batch_id"] and self.s.batch_detail(batch["id"])["batch"]["revision"])
        self.assertFalse(failed["passed"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        passed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, current)
        self.assertTrue(passed["passed"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.close_deviation("qa", "qa", dev["id"], "调整灌装参数", current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        rw = self.s.plan_rework("operator", "operator", self.f1, batch["id"], "返工包装", current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.complete_rework("operator", "operator", self.f1, rw["id"], current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.record_stability("lab", "lab", self.f1, batch["id"], "25C/60RH", "3m", 99, 105, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        result = self.s.decide("qa", "qa", batch["id"], "release", "调查关闭，复测合格", current)
        self.assertEqual("released", result["batch"]["state"])
        self.assertEqual(1, len(result["batch"] and self.s.batch_detail(batch["id"])["decisions"]))

    def test_critical_block_conditional_exception_and_factory_conflict(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-2", "胶囊", "2026-02-01", "2028-02-01")
        current = batch["revision"]
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        crit = self.s.add_deviation("inspector", "inspector", self.f1, batch["id"], "critical", "无菌数据异常", self.future, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as blocked:
            self.s.decide("qa", "qa", batch["id"], "release", "尝试放行", current)
        self.assertIn("关键偏差", blocked.exception.message)
        with self.assertRaises(ApiError):
            self.s.approve_exception("qa", "qa", crit["id"], "暂时接受", self.future, current)
        with self.assertRaises(ApiError):
            self.s.record_test("lab", "lab", self.f2, batch["id"], "水分", 1, 0, 2, current)
        with self.assertRaises(ApiError) as stale:
            self.s.record_test("lab", "lab", self.f1, batch["id"], "水分", 1, 0, 2, 1)
        self.assertEqual(409, stale.exception.status)

    def _released_batch(self, batch_no):
        batch = self.s.create_batch("operator", "operator", self.f1, batch_no, "注射液", "2026-03-01", "2028-03-01")
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, batch["revision"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.decide("qa", "qa", batch["id"], "release", "检验合格放行", current)
        return self.s.batch_detail(batch["id"])["batch"]

    def test_signal_register_freezes_and_maintain_keeps_release(self):
        batch = self._released_batch("B-3")
        with self.assertRaises(ApiError) as forbidden:
            self.s.register_signal("op", "operator", batch["id"], "retest_failure", "留样复测", "2026-09-01", "含量偏低", batch["revision"])
        self.assertEqual(403, forbidden.exception.status)
        with self.assertRaises(ApiError) as invalid:
            self.s.register_signal("qa", "qa", batch["id"], "retest_failure", "", "2026-09-01", "含量偏低", batch["revision"])
        self.assertEqual(400, invalid.exception.status)
        sig = self.s.register_signal("qa", "qa", batch["id"], "retest_failure", "留样复测", "2026-09-01", "复测含量低于标准", batch["revision"])
        self.assertEqual("pending", sig["status"])
        detail = self.s.batch_detail(batch["id"])
        self.assertTrue(detail["batch"]["frozen"])
        self.assertEqual(1, len(detail["signals"]))
        with self.assertRaises(ApiError) as dup:
            self.s.register_signal("qa", "qa", batch["id"], "other", "投诉", "2026-09-02", "再次", detail["batch"]["revision"])
        self.assertEqual(409, dup.exception.status)
        with self.assertRaises(ApiError) as stale:
            self.s.review_signal("qa", "qa", sig["id"], "maintain", "", 1)
        self.assertEqual(409, stale.exception.status)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        out = self.s.review_signal("qa", "qa", sig["id"], "maintain", "", current)
        self.assertEqual("released", out["batch"]["state"])
        self.assertFalse(out["batch"]["frozen"])
        decision = self.s.batch_detail(batch["id"])["decisions"][-1]
        self.assertIsNone(decision["invalidated_at"])

    def test_signal_return_to_investigation_invalidates_release(self):
        batch = self._released_batch("B-4")
        sig = self.s.register_signal("qa", "qa", batch["id"], "stability_oor", "稳定性考察", "2026-09-01", "3月点超限", batch["revision"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as no_reason:
            self.s.review_signal("qa", "qa", sig["id"], "return", "", current)
        self.assertEqual(400, no_reason.exception.status)
        out = self.s.review_signal("qa", "qa", sig["id"], "return", "稳定性超限需调查", current)
        self.assertEqual("investigation", out["batch"]["state"])
        detail = self.s.batch_detail(batch["id"])
        decision = detail["decisions"][-1]
        self.assertIsNotNone(decision["invalidated_at"])
        self.assertEqual("稳定性超限需调查", decision["invalidation_reason"])
        self.assertEqual("returned", detail["signals"][0]["status"])
        with self.assertRaises(ApiError) as again:
            self.s.review_signal("qa", "qa", sig["id"], "rescind", "重复处置", out["batch"]["revision"])
        self.assertEqual(409, again.exception.status)

    def test_signal_rescind_release(self):
        batch = self._released_batch("B-5")
        sig = self.s.register_signal("qa", "qa", batch["id"], "retest_failure", "投诉复测", "2026-09-01", "含量不合格", batch["revision"])
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        out = self.s.review_signal("qa", "qa", sig["id"], "rescind", "确认复测不合格，撤销放行", current)
        self.assertEqual("rejected", out["batch"]["state"])
        decision = self.s.batch_detail(batch["id"])["decisions"][-1]
        self.assertIsNotNone(decision["invalidated_at"])
        self.assertEqual("确认复测不合格，撤销放行", decision["invalidation_reason"])

    def test_signal_requires_released_batch(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-6", "颗粒", "2026-01-01", "2028-01-01")
        with self.assertRaises(ApiError) as not_released:
            self.s.register_signal("qa", "qa", batch["id"], "other", "投诉", "2026-09-01", "影响", batch["revision"])
        self.assertEqual(409, not_released.exception.status)


if __name__ == "__main__": unittest.main()
