"""归并发布决策的纯函数测试：幂等、原子切换与矛盾保留（不依赖数据库）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain.consolidation import (  # noqa: E402
    MergeConflict,
    plan_publication,
    switch_active,
)


def _raw_rows():
    return [
        {"report_ref": "R1", "site_id": "LH-01", "source": "热线",
         "occurred_at": "2026-11-14T09:00:00", "legacy_order": None},
        {"report_ref": "R2", "site_id": "LH-01", "source": "巡检",
         "occurred_at": "2026-11-14T09:15:00", "legacy_order": None},
    ]


class PlanPublicationTests(unittest.TestCase):
    def test_first_publish(self):
        action, payload, next_no, stats = plan_publication(
            _raw_rows(), [], [], "fp-1")
        self.assertEqual(action, "publish")
        self.assertEqual(next_no, 1)
        self.assertEqual(stats["order_count"], 1)
        self.assertEqual(payload["statistics"]["order_count"], 1)

    def test_same_fingerprint_is_reused_not_regenerated(self):
        existing = [{"version_no": 1, "input_fingerprint": "fp-1", "is_active": True}]
        action, payload, next_no, stats = plan_publication(
            _raw_rows(), [], existing, "fp-1")
        self.assertEqual(action, "reuse")
        self.assertEqual(payload["version_no"], 1)
        self.assertIsNone(stats)
        # 不同指纹才会重新归并。
        action2, _, next_no2, _ = plan_publication(_raw_rows(), [], existing, "fp-2")
        self.assertEqual(action2, "publish")
        self.assertEqual(next_no2, 2)

    def test_conflict_returns_contradictions_without_publish(self):
        rows = _raw_rows()
        rows.append({"report_ref": "R3", "site_id": "LH-01", "source": "热线",
                     "occurred_at": "2026-11-14T10:30:00", "legacy_order": None})
        judgments = [{"left_ref": "R1", "right_ref": "R3", "relation": "same"}]
        action, payload, next_no, stats = plan_publication(
            rows, judgments, [{"version_no": 1, "input_fingerprint": "old",
                               "is_active": True}], "fp-new")
        self.assertEqual(action, "conflict")
        self.assertIsNone(next_no)
        self.assertTrue(payload)
        self.assertEqual(payload[0]["type"], "same_exceeds_window")

    def test_new_report_increases_versions_and_orders(self):
        existing = [{"version_no": 1, "input_fingerprint": "fp-1", "is_active": True}]
        rows = _raw_rows()
        rows.append({"report_ref": "R3", "site_id": "LH-01", "source": "巡检",
                     "occurred_at": "2026-11-14T11:00:00", "legacy_order": None})
        action, payload, next_no, stats = plan_publication(
            rows, [], existing, "fp-2")
        self.assertEqual(action, "publish")
        self.assertEqual(next_no, 2)
        self.assertEqual(stats["order_count"], 2)


class SwitchActiveTests(unittest.TestCase):
    def test_only_new_version_active(self):
        versions = [
            {"version_no": 1, "is_active": True},
            {"version_no": 2, "is_active": False},
        ]
        flags = switch_active(versions, 2)
        self.assertEqual(flags, [False, True])

    def test_switch_plan_is_complete_and_ordered(self):
        versions = [{"version_no": n} for n in range(1, 4)]
        flags = switch_active(versions, 3)
        self.assertEqual(flags, [False, False, True])
        self.assertEqual(len(flags), len(versions))


class MergeConflictContractTests(unittest.TestCase):
    def test_conflict_exception_carries_contradictions(self):
        # 保证直接调用底层归并时，矛盾仍以异常 + contradictions 形式暴露。
        from src.domain.consolidation import merge_reports
        rows = _raw_rows()
        rows.append({"report_ref": "R3", "site_id": "LH-01", "source": "热线",
                     "occurred_at": "2026-11-14T10:30:00"})
        with self.assertRaises(MergeConflict) as ctx:
            merge_reports(rows, [{"left_ref": "R1", "right_ref": "R3", "relation": "same"}])
        self.assertTrue(ctx.exception.contradictions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
