import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.payload = {
            "frequency_mhz": 5800.0,
            "bandwidth_mhz": 10.0,
            "station_id": "ST-02",
            "region": "west",
            "strength_dbm": -55,
            "detected_at": "2026-09-27T11:00:00+00:00",
            "reporter": "monitor-2",
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_duplicate_permission_and_region(self):
        item = self.service.create_item(self.payload, "m", "monitor")
        # 同一频段的重复上报不再报错，而是并进已有事件，两条上报都留下。
        merged = self.service.create_item(self.payload, "m", "monitor")
        self.assertEqual(merged["id"], item["id"])
        self.assertEqual(len(merged["payload"]["reports"]), 1)
        # 两个监测站同时上报同一频段，后到的一条并进已有事件，各自测到的强度、时间和来源都留下。
        other = dict(self.payload, station_id="ST-03", reporter="monitor-3", strength_dbm=-68)
        merged = self.service.create_item(other, "m3", "monitor")
        self.assertEqual(merged["id"], item["id"])
        self.assertEqual(len(merged["payload"]["reports"]), 2)
        stations = {r["station_id"] for r in merged["payload"]["reports"]}
        self.assertEqual(stations, {"ST-02", "ST-03"})
        strengths = {r["station_id"]: r["strength_dbm"] for r in merged["payload"]["reports"]}
        self.assertEqual(strengths["ST-02"], -55)
        self.assertEqual(strengths["ST-03"], -68)
        # 合并后版本递增，用最新版本继续操作。
        item = self.service.get_item(item["id"])
        item = self.service.act(item["id"], "assess", {}, "m", "monitor", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "x", "confidence": 0.8}, "f", "field_operator", item["version"])
        with self.assertRaises(DomainError) as forbidden:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-X"}, "f", "field_operator", item["version"], "west")
        self.assertEqual(forbidden.exception.status, 403)
        with self.assertRaises(DomainError) as mismatch:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-X"}, "c", "coordinator", item["version"], "east")
        self.assertEqual(mismatch.exception.code, "region_mismatch")

    def test_measurement_revision_preserves_original_and_version_conflict(self):
        item = self.service.create_item(self.payload, "m", "monitor")
        revised = self.service.act(item["id"], "correct_measurement", {"strength_dbm": -72, "reason": "sensor drift"}, "m", "monitor", item["version"])
        self.assertEqual(revised["payload"]["measurement_revisions"][0]["old_strength_dbm"], -55.0)
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "assess", {}, "m", "monitor", item["version"])


if __name__ == "__main__":
    unittest.main()
