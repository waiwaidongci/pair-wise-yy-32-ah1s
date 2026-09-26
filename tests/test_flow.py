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


class PostMarketSignalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = BatchService(Store(Path(self.tmp.name) / "s.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def released_batch(self, batch_no="B-9"):
        batch = self.s.create_batch("operator", "operator", self.f1, batch_no, "注射液", "2026-01-01", "2028-01-01")
        current = batch["revision"]
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        self.s.decide("qa", "qa", batch["id"], "release", "检验合格，正式放行", current)
        return self.s.batch_detail(batch["id"])["batch"]

    def register(self, batch, **overrides):
        args = {"signal_type": "retest_failure", "source": "留样复测", "discovered_at": "2020-01-01T00:00:00Z",
                "impact": "复测含量低于标准，涉及已放行批次"}
        args.update(overrides)
        return self.s.register_signal("qa", "qa", batch["id"], args["signal_type"], args["source"],
                                      args["discovered_at"], args["impact"], batch["revision"])

    def test_registration_validation_and_freeze_flag(self):
        batch = self.released_batch()
        with self.assertRaises(ApiError) as not_qa:
            self.s.register_signal("lab", "lab", batch["id"], "retest_failure", "留样", "2020-01-01T00:00:00Z", "影响", batch["revision"])
        self.assertEqual(403, not_qa.exception.status)
        with self.assertRaises(ApiError):  # 缺影响说明
            self.s.register_signal("qa", "qa", batch["id"], "retest_failure", "留样", "2020-01-01T00:00:00Z", "", batch["revision"])
        with self.assertRaises(ApiError):  # 类型不合法
            self.s.register_signal("qa", "qa", batch["id"], "unknown", "留样", "2020-01-01T00:00:00Z", "影响", batch["revision"])
        with self.assertRaises(ApiError):  # 发现时间在未来
            self.s.register_signal("qa", "qa", batch["id"], "retest_failure", "留样", "2999-01-01T00:00:00Z", "影响", batch["revision"])
        with self.assertRaises(ApiError):  # 发现时间格式不合法
            self.s.register_signal("qa", "qa", batch["id"], "other", "投诉", "not-a-date", "影响", batch["revision"])
        signal = self.register(batch)
        self.assertEqual("pending", signal["status"])
        detail = self.s.batch_detail(batch["id"])
        self.assertTrue(detail["batch"]["frozen"])
        with self.assertRaises(ApiError):  # 信号登记推进了修订号，旧号再登记被拒
            self.s.register_signal("qa", "qa", batch["id"], "other", "投诉", "2020-01-01T00:00:00Z", "影响", batch["revision"])
        other = self.s.create_batch("operator", "operator", self.f1, "B-10", "药片", "2026-01-01", "2028-01-01")
        with self.assertRaises(ApiError) as not_released:  # 未放行批次不能登记信号
            self.s.register_signal("qa", "qa", other["id"], "other", "投诉", "2020-01-01T00:00:00Z", "影响", other["revision"])
        self.assertEqual(409, not_released.exception.status)

    def test_maintain_keeps_release_and_decision_valid(self):
        batch = self.released_batch()
        self.register(batch)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        signal = self.s.batch_detail(batch["id"])["signals"][0]
        result = self.s.assess_signal("qa", "qa", signal["id"], "maintain", "复测为孤立偏差，影响可控", current)
        self.assertEqual("released", result["batch"]["state"])
        detail = self.s.batch_detail(batch["id"])
        self.assertFalse(detail["batch"]["frozen"])
        self.assertEqual("assessed", detail["signals"][0]["status"])
        self.assertIsNone(detail["decisions"][0]["voided_at"])

    def test_investigate_voids_original_decision_and_allows_release_again(self):
        batch = self.released_batch()
        self.register(batch)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        signal = self.s.batch_detail(batch["id"])["signals"][0]
        result = self.s.assess_signal("qa", "qa", signal["id"], "investigate", "留样复测不合格，需调查", current)
        self.assertEqual("investigation", result["batch"]["state"])
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual("留样复测不合格，需调查", detail["decisions"][0]["void_reason"])
        self.assertEqual("qa", detail["decisions"][0]["voided_by"])
        self.assertIsNotNone(detail["decisions"][0]["voided_at"])
        current = detail["batch"]["revision"]
        again = self.s.decide("qa", "qa", batch["id"], "release", "调查关闭，重新放行", current)
        self.assertEqual("released", again["batch"]["state"])
        detail = self.s.batch_detail(batch["id"])
        self.assertIsNone(detail["decisions"][1]["voided_at"])

    def test_revoke_is_terminal_and_voids_decision(self):
        batch = self.released_batch()
        self.register(batch, signal_type="stability_oor", source="稳定性考察", impact="3月点超限")
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        signal = self.s.batch_detail(batch["id"])["signals"][0]
        result = self.s.assess_signal("qa", "qa", signal["id"], "revoke", "确认影响患者安全，撤销放行", current)
        self.assertEqual("revoked", result["batch"]["state"])
        detail = self.s.batch_detail(batch["id"])
        self.assertIsNotNone(detail["decisions"][0]["voided_at"])
        self.assertEqual("revoke", detail["signals"][0]["assessment"])
        current = detail["batch"]["revision"]
        with self.assertRaises(ApiError):  # 撤销后为终态，不能再作放行决定
            self.s.decide("qa", "qa", batch["id"], "release", "再次放行", current)
        with self.assertRaises(ApiError):  # 终态不能补录检验
            self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, current)
        with self.assertRaises(ApiError):  # 终态不能新增偏差
            self.s.add_deviation("operator", "operator", self.f1, batch["id"], "minor", "新增偏差", None, current)

    def test_pending_signal_freezes_decisions_until_all_assessed(self):
        batch = self.released_batch()
        s1 = self.register(batch)
        batch = self.s.batch_detail(batch["id"])["batch"]
        s2 = self.register(batch, signal_type="stability_oor", source="稳定性考察", impact="3月点超限")
        batch = self.s.batch_detail(batch["id"])["batch"]
        r1 = self.s.assess_signal("qa", "qa", s1["id"], "investigate", "复测确认不合格，转回调查", batch["revision"])
        self.assertEqual("investigation", r1["batch"]["state"])
        current = r1["batch"]["revision"]
        with self.assertRaises(ApiError) as frozen:  # 仍有待评估信号，决定先停下
            self.s.decide("qa", "qa", batch["id"], "release", "尝试再次放行", current)
        self.assertIn("冻结", frozen.exception.message)
        with self.assertRaises(ApiError):  # 批次已不在放行状态，不能维持放行
            self.s.assess_signal("qa", "qa", s2["id"], "maintain", "试图维持", current)
        r2 = self.s.assess_signal("qa", "qa", s2["id"], "investigate", "稳定性超限一并调查", current)
        current = r2["batch"]["revision"]
        again = self.s.decide("qa", "qa", batch["id"], "release", "调查关闭，重新放行", current)
        self.assertEqual("released", again["batch"]["state"])

    def test_assess_requires_fresh_revision_and_single_assessment(self):
        batch = self.released_batch()
        signal = self.register(batch)
        with self.assertRaises(ApiError) as stale:  # 登记后版本已变，必须重新加载
            self.s.assess_signal("qa", "qa", signal["id"], "maintain", "维持放行", batch["revision"])
        self.assertEqual(409, stale.exception.status)
        self.assertIn("重新加载", stale.exception.message)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError):  # 缺复评原因
            self.s.assess_signal("qa", "qa", signal["id"], "maintain", "", current)
        self.s.assess_signal("qa", "qa", signal["id"], "maintain", "维持放行", current)
        current = self.s.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as again:  # 信号只能复评一次
            self.s.assess_signal("qa", "qa", signal["id"], "revoke", "反悔", current)
        self.assertEqual(409, again.exception.status)


if __name__ == "__main__": unittest.main()
