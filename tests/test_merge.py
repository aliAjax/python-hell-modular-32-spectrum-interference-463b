import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


def report_payload(station, region, frequency, detected_at, strength, reporter):
    return {
        "frequency_mhz": frequency,
        "bandwidth_mhz": 10.0,
        "station_id": station,
        "region": region,
        "strength_dbm": strength,
        "detected_at": detected_at,
        "reporter": reporter,
    }


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.first = report_payload("ST-01", "north", 100.0, "2026-09-27T10:00:00+00:00", -40, "monitor-1")
        self.second = report_payload("ST-02", "east", 104.0, "2026-09-27T10:00:30+00:00", -52, "monitor-2")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_same_band_report_merges_and_keeps_both_measurements(self):
        item = self.service.create_item(self.first, "m1", "monitor")
        merged = self.service.create_item(self.second, "m2", "monitor")
        self.assertEqual(merged["id"], item["id"])
        self.assertEqual(len(self.service.list_items()), 1)
        reports = merged["payload"]["reports"]
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0]["strength_dbm"], -40.0)
        self.assertEqual(reports[0]["detected_at"], "2026-09-27T10:00:00+00:00")
        self.assertEqual(reports[0]["station_id"], "ST-01")
        self.assertEqual(reports[1]["strength_dbm"], -52.0)
        self.assertEqual(reports[1]["detected_at"], "2026-09-27T10:00:30+00:00")
        self.assertEqual(reports[1]["station_id"], "ST-02")
        self.assertEqual(merged["payload"]["strength_dbm"], -52.0)
        sources = self.repo.list_sources(item["id"])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["payload"]["station_id"], "ST-02")
        self.assertEqual(sources[0]["observed_at"], "2026-09-27T10:00:30+00:00")
        events = [event["event_type"] for event in self.repo.audit_trail(item["id"])]
        self.assertEqual(events, ["created", "merged"])

    def test_merge_invalidates_assessment_and_location(self):
        same_region = dict(self.second, region="north")
        item = self.service.create_item(self.first, "m1", "monitor")
        item = self.service.act(item["id"], "assess", {}, "a1", "analyst", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "cell-3", "confidence": 0.9}, "f1", "field_operator", item["version"])
        self.assertEqual(item["status"], "located")
        merged = self.service.create_item(same_region, "m2", "monitor")
        self.assertEqual(merged["status"], "pending")
        self.assertNotIn("assessment", merged["payload"])
        self.assertNotIn("location", merged["payload"])
        with self.assertRaises(DomainError) as context:
            self.service.act(merged["id"], "suspend", {"authorization_code": "REG-1"}, "c1", "coordinator", merged["version"], "north")
        self.assertEqual(context.exception.code, "invalid_state")
        item = self.service.act(merged["id"], "assess", {}, "a1", "analyst", merged["version"])
        self.assertEqual(item["payload"]["assessment"], item["assessment"])
        item = self.service.act(item["id"], "locate", {"location": "cell-9", "confidence": 0.8}, "f1", "field_operator", item["version"])
        self.assertEqual(item["status"], "located")

    def test_correct_measurement_invalidates_assessment_and_location(self):
        item = self.service.create_item(self.first, "m1", "monitor")
        item = self.service.act(item["id"], "assess", {}, "a1", "analyst", item["version"])
        item = self.service.act(item["id"], "locate", {"location": "cell-3", "confidence": 0.9}, "f1", "field_operator", item["version"])
        revised = self.service.act(item["id"], "correct_measurement", {"strength_dbm": -70, "reason": "sensor drift"}, "m1", "monitor", item["version"])
        self.assertEqual(revised["status"], "pending")
        self.assertNotIn("assessment", revised["payload"])
        self.assertNotIn("location", revised["payload"])
        self.assertEqual(revised["payload"]["measurement_revisions"][0]["old_strength_dbm"], -40.0)

    def test_cross_region_record_requires_regulator(self):
        item = self.service.create_item(self.first, "m1", "monitor")
        merged = self.service.create_item(self.second, "m2", "monitor")
        self.assertEqual(merged["payload"]["regions"], ["east", "north"])
        item = self.service.act(merged["id"], "assess", {}, "a1", "analyst", merged["version"])
        item = self.service.act(item["id"], "locate", {"location": "cell-3", "confidence": 0.9}, "f1", "field_operator", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-1"}, "c1", "coordinator", item["version"], "north")
        self.assertEqual(context.exception.code, "regulator_required")
        self.assertEqual(context.exception.status, 403)
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-1"}, "r1", "regulator", item["version"], "north")
        self.assertEqual(item["status"], "suspended")

    def test_failed_merge_rolls_back_and_retry_succeeds(self):
        item = self.service.create_item(self.first, "m1", "monitor")
        with mock.patch.object(self.repo, "append_audit", side_effect=sqlite3.Error("simulated merge failure")):
            with self.assertRaises(sqlite3.Error):
                self.service.create_item(self.second, "m2", "monitor")
        after = self.service.get_item(item["id"])
        self.assertEqual(after["version"], item["version"])
        self.assertEqual(len(after["payload"]["reports"]), 1)
        self.assertEqual(self.repo.list_sources(item["id"]), [])
        merged = self.service.create_item(self.second, "m2", "monitor")
        self.assertEqual(merged["id"], item["id"])
        self.assertEqual(len(merged["payload"]["reports"]), 2)
        again = self.service.create_item(self.second, "m2", "monitor")
        self.assertEqual(again["id"], item["id"])
        self.assertEqual(len(again["payload"]["reports"]), 2)

    def test_concurrent_reports_merge_into_single_event(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def submit(payload, actor):
            try:
                barrier.wait()
                results.append(self.service.create_item(payload, actor, "monitor"))
            except Exception as exc:  # noqa: BLE001 - surfaced via assertion
                errors.append(exc)

        first = threading.Thread(target=submit, args=(self.first, "m1"))
        second = threading.Thread(target=submit, args=(self.second, "m2"))
        first.start()
        second.start()
        first.join()
        second.join()
        self.assertEqual(errors, [])
        self.assertEqual({item["id"] for item in results}, {results[0]["id"]})
        final = self.service.get_item(results[0]["id"])
        self.assertEqual(len(final["payload"]["reports"]), 2)
        self.assertEqual(len(self.service.list_items()), 1)


if __name__ == "__main__":
    unittest.main()
