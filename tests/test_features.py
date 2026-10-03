import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class FeatureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _payload(self, **over):
        base = {
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -45,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }
        base.update(over)
        return base

    def test_two_stations_same_band_merge(self):
        # 两个监测站同时上报同一频段，后到的一条并进已有事件，各自测到的强度、时间和来源都留下。
        item = self.service.create_item(self._payload(), "m1", "monitor")
        other = self._payload(station_id="ST-02", reporter="monitor-2", strength_dbm=-52)
        merged = self.service.create_item(other, "m2", "monitor")
        self.assertEqual(merged["id"], item["id"])
        self.assertEqual(len(merged["payload"]["reports"]), 2)
        by_station = {r["station_id"]: r for r in merged["payload"]["reports"]}
        self.assertEqual(by_station["ST-01"]["strength_dbm"], -45)
        self.assertEqual(by_station["ST-02"]["strength_dbm"], -52)
        self.assertEqual(by_station["ST-01"]["reporter"], "monitor-1")
        self.assertEqual(by_station["ST-02"]["reporter"], "monitor-2")
        # 顶层更新为最新一条
        self.assertEqual(merged["payload"]["strength_dbm"], -52)
        self.assertEqual(merged["payload"]["station_id"], "ST-02")

    def test_merge_retry_is_idempotent(self):
        # 合并失败后值班员可以重试，原事件和两条上报都还在。
        item = self.service.create_item(self._payload(), "m1", "monitor")
        other = self._payload(station_id="ST-02", reporter="monitor-2", strength_dbm=-52)
        merged1 = self.service.create_item(other, "m2", "monitor")
        # 重试同一条上报，不应重复写入。
        merged2 = self.service.create_item(other, "m2", "monitor")
        self.assertEqual(merged2["id"], item["id"])
        self.assertEqual(len(merged2["payload"]["reports"]), 2)
        # 值班员角色也能重试
        merged3 = self.service.create_item(other, "duty", "duty_officer")
        self.assertEqual(len(merged3["payload"]["reports"]), 2)

    def test_measurement_update_invalidates_assessment_and_location(self):
        # 测量数据更新后，评估和定位立即失效，处置人要按最新结果重新确认。
        item = self.service.create_item(self._payload(), "m1", "monitor")
        item = self.service.act(item["id"], "assess", {}, "a", "analyst", item["version"])
        self.assertIn("assessment", item["payload"])
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "f", "field_operator", item["version"])
        self.assertEqual(item["status"], "located")
        self.assertIn("location", item["payload"])
        # 更正测量数据
        item = self.service.act(item["id"], "correct_measurement", {"strength_dbm": -70, "reason": "sensor drift"}, "m", "monitor", item["version"])
        self.assertEqual(item["status"], "pending")
        self.assertNotIn("assessment", item["payload"])
        self.assertNotIn("location", item["payload"])
        # 处置人按最新结果重新确认
        item = self.service.act(item["id"], "assess", {}, "a", "analyst", item["version"])
        self.assertIn("assessment", item["payload"])
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "f", "field_operator", item["version"])
        self.assertEqual(item["status"], "located")

    def test_merge_invalidates_assessment_and_location(self):
        # 合并进来新的测量数据后，评估和定位同样失效，需要按最新结果重新确认。
        item = self.service.create_item(self._payload(), "m1", "monitor")
        item = self.service.act(item["id"], "assess", {}, "a", "analyst", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "f", "field_operator", item["version"])
        self.assertEqual(item["status"], "located")
        other = self._payload(station_id="ST-02", reporter="monitor-2", strength_dbm=-60)
        merged = self.service.create_item(other, "m2", "monitor")
        self.assertEqual(merged["status"], "pending")
        self.assertNotIn("assessment", merged["payload"])
        self.assertNotIn("location", merged["payload"])

    def test_cross_region_requires_regulator_review(self):
        # 跨区记录由监管角色复核，普通协调员越权处理要被拒绝。
        item = self.service.create_item(self._payload(), "m1", "monitor")
        # 监管角色可以录入其他区域的来源，记录因此标记为跨区。
        self.service.add_source(item["id"], {
            "source_type": "station_report",
            "external_id": "ST-09",
            "observed_at": "2026-09-27T11:00:00+00:00",
            "strength_dbm": -50,
            "region": "south",
        }, "r", "regulator", "north")
        item = self.service.get_item(item["id"])
        self.assertTrue(item["payload"]["cross_region"])
        # 普通协调员（本区域）处理跨区记录被拒绝。
        item = self.service.act(item["id"], "assess", {}, "a", "analyst", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "x", "confidence": 0.8}, "f", "field_operator", item["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-N"}, "c", "coordinator", item["version"], "north")
        self.assertEqual(ctx.exception.code, "cross_region_review_required")
        # 监管角色复核后才能处理。
        item = self.service.act(item["id"], "review_cross_region", {}, "r", "regulator", item["version"])
        self.assertTrue(item["payload"]["cross_region_reviewed"])
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-N"}, "c", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "suspended")

    def test_cross_region_coordinator_overstep_rejected(self):
        # 普通协调员越权处理其他区域记录被拒绝。
        item = self.service.create_item(self._payload(), "m1", "monitor")
        item = self.service.act(item["id"], "assess", {}, "a", "analyst", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "x", "confidence": 0.8}, "f", "field_operator", item["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-N"}, "c", "coordinator", item["version"], "east")
        self.assertEqual(ctx.exception.code, "region_mismatch")

    def test_audit_trail_preserved_after_merge(self):
        # 合并后审计链仍然完整可查。
        item = self.service.create_item(self._payload(), "m1", "monitor")
        other = self._payload(station_id="ST-02", reporter="monitor-2", strength_dbm=-52)
        self.service.create_item(other, "m2", "monitor")
        trail = self.repo.audit_trail(item["id"])
        types = [e["event_type"] for e in trail]
        self.assertIn("created", types)
        self.assertIn("report_merged", types)


if __name__ == "__main__":
    unittest.main()
