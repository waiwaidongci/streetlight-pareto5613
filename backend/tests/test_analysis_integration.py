"""归并版本驱动集中度测算的集成测试（纯标准库，不依赖数据库）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain.analysis import analyze, validate_rows  # noqa: E402
from src.domain.consolidation import (  # noqa: E402
    MergeConflict,
    legacy_pair_weights,
    merge_reports,
    normalize_reports,
    serialize,
)
from src.domain.demo import demo_rows  # noqa: E402


def _register(site_id, district, name, fixtures):
    return {"record_type": "灯组台账", "site_id": site_id, "district": district,
            "site_name": name, "fixture_count": fixtures,
            "incident_id": None, "report_date": None,
            "report_ref": None, "source": None, "occurred_at": None, "legacy_order": None}


def _report(ref, site_id, source, occurred_at, legacy=None):
    return {"record_type": "原始报障", "site_id": site_id,
            "district": None, "site_name": None, "fixture_count": None,
            "incident_id": None, "report_date": None,
            "report_ref": ref, "source": source, "occurred_at": occurred_at,
            "legacy_order": legacy}


class DemoDataTests(unittest.TestCase):
    def test_demo_rows_validate(self):
        records = validate_rows(demo_rows())
        kinds = {row["record_type"] for row in records}
        self.assertIn("原始报障", kinds)
        self.assertIn("灯组台账", kinds)

    def test_demo_reports_merge_hotline_and_patrol(self):
        rows = demo_rows()
        raw = [r for r in rows if r["record_type"] == "原始报障"]
        result = merge_reports(raw)
        # LH-01 的 14 日 20:05 热线与 20:18 巡检相差 13 分钟，必须同单。
        lh01 = [o for o in result["orders"] if o.site_id == "LH-01"]
        merged = next(o for o in lh01 if {r.ref for r in o.reports} >= {"RP-2611001", "RP-2611002"})
        self.assertEqual(merged.span_minutes, 13)
        self.assertEqual({r.source for r in merged.reports}, {"热线", "巡检"})


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            _register("LH-01", "滨河片区", "测试路口", 10),
            _register("LH-02", "老城片区", "测试巷", 4),
            # LH-01：9:00 热线、9:20 巡检（同故障），10:00 热线（独立故障，超窗口）
            _report("R1", "LH-01", "热线", "2026-11-14T09:00:00"),
            _report("R2", "LH-01", "巡检", "2026-11-14T09:20:00"),
            _report("R3", "LH-01", "热线", "2026-11-14T10:00:00"),
            # LH-02：单条
            _report("R4", "LH-02", "巡检", "2026-11-15T08:00:00"),
        ]
        validate_rows(self.rows)

    def _consolidation_payload(self, judgments=None, pair_weight=None):
        raw = [r for r in self.rows if r["record_type"] == "原始报障"]
        reports = normalize_reports(raw)
        weights = pair_weight if pair_weight is not None else legacy_pair_weights(reports)
        result = merge_reports(raw, judgments, pair_weight=weights)
        payload = serialize(result)
        payload["version_id"] = 1
        payload["version_no"] = 1
        return payload

    def test_analysis_uses_consolidated_orders(self):
        payload = self._consolidation_payload()
        # 3 条 LH-01 报障归并成 2 单，LH-02 1 单，共 3 单。
        self.assertEqual(payload["statistics"]["order_count"], 3)
        params = {"top_percent": 20, "min_incidents": 2, "min_rate": 40}
        result = analyze(self.rows, params, consolidation=payload)
        # 集中度按工单统计：LH-01 有 2 单。
        lh01 = next(row for row in result["tables"][0]["rows"] if row["site_id"] == "LH-01")
        self.assertEqual(lh01["incident_count"], 2)
        self.assertEqual(result["metrics"][1]["value"], 3)
        self.assertEqual(result["consolidation"]["version_no"], 1)

    def test_trace_links_order_to_raw_reports(self):
        payload = self._consolidation_payload()
        params = {"top_percent": 20, "min_incidents": 2, "min_rate": 40}
        result = analyze(self.rows, params, consolidation=payload)
        trace = result["tables"][2]
        column_keys = [column["key"] for column in trace["columns"]]
        self.assertIn("raw_report_refs", column_keys)
        merged_row = next(row for row in trace["rows"]
                          if "R1" in row.get("raw_report_refs", ""))
        self.assertEqual(merged_row["raw_report_count"], 2)
        self.assertEqual(merged_row["sources"], "热线、巡检")

    def test_recompute_same_input_is_identical(self):
        raw = [r for r in self.rows if r["record_type"] == "原始报障"]
        first = serialize(merge_reports(raw))
        second = serialize(merge_reports(raw))
        self.assertEqual(first, second)

    def test_manual_judgment_changes_orders_atomically(self):
        # 人工认定 R2、R3 属不同故障（它们本就跨窗口分单），再认定 R1、R3 同故障会矛盾。
        raw = [r for r in self.rows if r["record_type"] == "原始报障"]
        with self.assertRaises(MergeConflict):
            merge_reports(raw, [
                {"left_ref": "R1", "right_ref": "R2", "relation": "same"},
                {"left_ref": "R1", "right_ref": "R3", "relation": "same"},
            ])

    def test_conflict_keeps_contradiction_detail(self):
        raw = [r for r in self.rows if r["record_type"] == "原始报障"]
        try:
            merge_reports(raw, [
                {"left_ref": "R1", "right_ref": "R3", "relation": "same"},
            ])
        except MergeConflict as conflict:
            self.assertTrue(conflict.contradictions)
            self.assertEqual(conflict.contradictions[0]["type"], "same_exceeds_window")
        else:
            self.fail("应当抛出归并矛盾")

    def test_legacy_without_consolidation_still_works(self):
        # 没有归并版本时，旧的暗区工单行口径不受影响。
        rows = [
            _register("LH-01", "滨河片区", "测试路口", 10),
            {"record_type": "暗区工单", "site_id": "LH-01",
             "district": None, "site_name": None, "fixture_count": None,
             "incident_id": "LT-1", "report_date": "2026-11-14",
             "report_ref": None, "source": None, "occurred_at": None, "legacy_order": None},
        ]
        params = {"top_percent": 20, "min_incidents": 2, "min_rate": 40}
        result = analyze(rows, params)
        self.assertIsNone(result["consolidation"])
        self.assertEqual(result["metrics"][1]["value"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
