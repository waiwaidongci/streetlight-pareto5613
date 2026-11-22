"""归并算法的单元测试，纯标准库运行：python3 -m unittest -v。"""

import sys
import unittest
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.domain.consolidation import (  # noqa: E402
    MAX_SEARCH_NODES,
    MergeConflict,
    WINDOW_MINUTES,
    legacy_pair_weights,
    merge_reports,
    normalize_reports,
    serialize,
)


def at(hour, minute=0, day=14):
    return datetime(2026, 11, day, hour, minute)


def report(ref, site, hour, minute=0, source="热线", legacy=None, day=14):
    return {
        "report_ref": ref,
        "site_id": site,
        "source": source,
        "occurred_at": f"2026-11-{day:02d}T{hour:02d}:{minute:02d}:00",
        "legacy_order": legacy,
    }


class MergeBasicTests(unittest.TestCase):
    def test_single_report_becomes_one_order(self):
        result = merge_reports([report("R1", "LH-01", 9, 0)])
        self.assertEqual(result["statistics"]["order_count"], 1)
        self.assertEqual(result["orders"][0].report_refs if hasattr(result["orders"][0], "report_refs")
                         else [r.ref for r in result["orders"][0].reports], ["R1"])

    def test_window_inside_half_hour_merges(self):
        # 9:00 与 9:30 相差恰好 30 分钟，必须合并为 1 单（边界含 30 分钟）。
        rows = [report("R1", "LH-01", 9, 0), report("R2", "LH-01", 9, 30)]
        result = merge_reports(rows)
        self.assertEqual(result["statistics"]["order_count"], 1)

    def test_window_beyond_half_hour_splits(self):
        rows = [report("R1", "LH-01", 9, 0), report("R2", "LH-01", 9, 31)]
        result = merge_reports(rows)
        self.assertEqual(result["statistics"]["order_count"], 2)

    def test_different_sites_never_merge_even_inside_window(self):
        rows = [report("R1", "LH-01", 9, 0), report("R2", "LH-02", 9, 5)]
        result = merge_reports(rows)
        self.assertEqual(result["statistics"]["order_count"], 2)
        self.assertEqual({order.site_id for order in result["orders"]}, {"LH-01", "LH-02"})

    def test_minimum_order_count_chain_merging(self):
        # 9:00、9:20、9:40：相邻 20 分钟但首尾 40 分钟，最优 2 单。
        rows = [report(f"R{i}", "LH-01", 9, minute)
                for i, minute in enumerate((0, 20, 40), start=1)]
        result = merge_reports(rows)
        self.assertEqual(result["statistics"]["order_count"], 2)

    def test_five_reports_two_windows(self):
        # 9:00,9:10,9:20 与 10:00,10:10：两个窗口，共 2 单。
        rows = [report("R1", "LH-01", 9, 0), report("R2", "LH-01", 9, 10),
                report("R3", "LH-01", 9, 20), report("R4", "LH-01", 10, 0),
                report("R5", "LH-01", 10, 10)]
        result = merge_reports(rows)
        self.assertEqual(result["statistics"]["order_count"], 2)
        sizes = sorted(len(order.reports) for order in result["orders"])
        self.assertEqual(sizes, [2, 3])


class TieBreakTests(unittest.TestCase):
    def test_prefers_keeping_legacy_pair_when_equal_count(self):
        # 4 条报障，时间 9:00/9:20/9:40/10:00。任何划分都至少 2 单，
        # 存在多种 2 单划法；旧版把 R1、R2 同单，应优先保留该对。
        rows = [
            report("R1", "LH-01", 9, 0, legacy="OLD-1"),
            report("R2", "LH-01", 9, 20, legacy="OLD-1"),
            report("R3", "LH-01", 9, 40, legacy="OLD-2"),
            report("R4", "LH-01", 10, 0, legacy="OLD-2"),
        ]
        reports = normalize_reports(rows)
        weights = legacy_pair_weights(reports)
        result = merge_reports(rows, pair_weight=weights)
        self.assertEqual(result["statistics"]["order_count"], 2)
        self.assertEqual(result["statistics"]["preserved_pairs"], 2)
        grouped = {frozenset(r.ref for r in order.reports) for order in result["orders"]}
        self.assertIn(frozenset({"R1", "R2"}), grouped)
        self.assertIn(frozenset({"R3", "R4"}), grouped)

    def test_count_minimized_before_pair_preservation(self):
        # 旧版 R1/R3 同单（跨窗口），但最小工单数要求 R1、R2 同单：
        # 工单数最少优先，旧版对只能放弃。
        rows = [
            report("R1", "LH-01", 9, 0, legacy="OLD-1"),
            report("R2", "LH-01", 9, 20, legacy="OLD-9"),
            report("R3", "LH-01", 9, 40, legacy="OLD-1"),
        ]
        reports = normalize_reports(rows)
        result = merge_reports(rows, pair_weight=legacy_pair_weights(reports))
        self.assertEqual(result["statistics"]["order_count"], 2)
        # 旧版 R1-R3 无法保留（窗口超限），保留对为 0 仍是最优工单数。
        self.assertEqual(result["statistics"]["preserved_pairs"], 0)


class JudgmentTests(unittest.TestCase):
    def test_manual_same_forces_merge_even_across_sources(self):
        rows = [report("R1", "LH-01", 9, 0, source="热线"),
                report("R2", "LH-01", 9, 20, source="巡检")]
        result = merge_reports(rows, [{"left_ref": "R1", "right_ref": "R2", "relation": "same"}])
        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual(result["statistics"]["order_count"], 1)

    def test_manual_separate_forces_split_inside_window(self):
        rows = [report("R1", "LH-01", 9, 0), report("R2", "LH-01", 9, 10)]
        result = merge_reports(rows, [{"left_ref": "R1", "right_ref": "R2", "relation": "separate"}])
        self.assertEqual(result["statistics"]["order_count"], 2)

    def test_same_and_separate_conflict_lists_contradiction(self):
        rows = [report("R1", "LH-01", 9, 0), report("R2", "LH-01", 9, 10)]
        judgments = [
            {"left_ref": "R1", "right_ref": "R2", "relation": "same"},
            {"left_ref": "R1", "right_ref": "R2", "relation": "separate"},
        ]
        with self.assertRaises(MergeConflict) as ctx:
            merge_reports(rows, judgments)
        self.assertEqual(ctx.exception.contradictions[0]["type"], "same_and_separate")

    def test_same_judgment_exceeding_window_is_contradiction(self):
        rows = [report("R1", "LH-01", 9, 0), report("R2", "LH-01", 10, 0)]
        with self.assertRaises(MergeConflict) as ctx:
            merge_reports(rows, [{"left_ref": "R1", "right_ref": "R2", "relation": "same"}])
        self.assertEqual(ctx.exception.contradictions[0]["type"], "same_exceeds_window")

    def test_same_judgment_cross_site_is_contradiction(self):
        rows = [report("R1", "LH-01", 9, 0), report("R2", "LH-02", 9, 5)]
        with self.assertRaises(MergeConflict) as ctx:
            merge_reports(rows, [{"left_ref": "R1", "right_ref": "R2", "relation": "same"}])
        self.assertEqual(ctx.exception.contradictions[0]["type"], "same_cross_site")

    def test_transitive_same_then_separate_conflict(self):
        rows = [report("R1", "LH-01", 9, 0), report("R2", "LH-01", 9, 10),
                report("R3", "LH-01", 9, 20)]
        judgments = [
            {"left_ref": "R1", "right_ref": "R2", "relation": "same"},
            {"left_ref": "R2", "right_ref": "R3", "relation": "same"},
            {"left_ref": "R1", "right_ref": "R3", "relation": "separate"},
        ]
        with self.assertRaises(MergeConflict):
            merge_reports(rows, judgments)

    def test_separate_judgment_raises_order_count(self):
        # 三条都在 30 分钟窗口内，本可 1 单；两条必分后至少 2 单。
        rows = [report("R1", "LH-01", 9, 0), report("R2", "LH-01", 9, 10),
                report("R3", "LH-01", 9, 20)]
        result = merge_reports(rows, [{"left_ref": "R1", "right_ref": "R2", "relation": "separate"}])
        self.assertEqual(result["statistics"]["order_count"], 2)


class BruteForceComparisonTests(unittest.TestCase):
    """对小规模随机实例，用集合划分暴力枚举验证求解器字典序最优。"""

    @staticmethod
    def _brute_force(times, cannot_link, weights):
        n = len(times)
        best = None  # (组数, -保留对)
        best_partition = None

        def feasible(group):
            members = [times[i] for i in group]
            if (max(members) - min(members)).total_seconds() > WINDOW_MINUTES * 60:
                return False
            for a, b in combinations(group, 2):
                if frozenset((a, b)) in cannot_link:
                    return False
            return True

        def score(group):
            total = 0
            for a, b in combinations(sorted(group), 2):
                total += weights.get((f"R{a+1}", f"R{b+1}"), 0)
            return total

        def search(cursor, groups):
            nonlocal best, best_partition
            if cursor == n:
                preserved = sum(score(group) for group in groups)
                key = (len(groups), -preserved)
                if best is None or key < best:
                    best = key
                    best_partition = [frozenset(g) for g in groups]
                return
            for group in groups:
                trial = group | {cursor}
                if feasible(trial):
                    backup = set(group)
                    group.add(cursor)
                    search(cursor + 1, groups)
                    group.clear()
                    group.update(backup)
            search(cursor + 1, groups + [{cursor}])

        search(0, [])
        return best[0], -best[1], best_partition

    def test_random_instances_match_brute_force(self):
        import random
        rng = random.Random(20261122)
        for trial in range(250):
            n = rng.randint(1, 7)
            base = datetime(2026, 11, 14, 9, 0)
            stamps = sorted(base + timedelta(minutes=rng.randint(0, 75)) for _ in range(n))
            rows = [report(f"R{i+1}", "LH-01", stamps[i].hour, stamps[i].minute,
                           day=stamps[i].day)
                    for i in range(n)]
            judgments = []
            cannot_link = set()
            for a, b in combinations(range(n), 2):
                if rng.random() < 0.18:
                    judgments.append({"left_ref": f"R{a+1}", "right_ref": f"R{b+1}",
                                      "relation": "separate"})
                    cannot_link.add(frozenset((a, b)))
            weights = {}
            for a, b in combinations(range(n), 2):
                if rng.random() < 0.3:
                    weights[(f"R{a+1}", f"R{b+1}")] = 1
            try:
                result = merge_reports(rows, judgments, pair_weight=weights)
            except MergeConflict:
                continue
            expected_count, expected_score, expected_partition = self._brute_force(
                stamps, cannot_link, weights)
            self.assertEqual(result["statistics"]["order_count"], expected_count,
                             f"trial {trial} 工单数不一致")
            self.assertEqual(result["statistics"]["preserved_pairs"], expected_score,
                             f"trial {trial} 保留对不一致")
            actual = {frozenset(r.ref for r in order.reports) for order in result["orders"]}
            # 最优划分可能不唯一，但组数与分数必须与暴力解一致。
            self.assertEqual(len(actual), expected_count)


class DeterminismTests(unittest.TestCase):
    def _rows(self):
        return [
            report("R1", "LH-01", 9, 0, source="热线"),
            report("R2", "LH-01", 9, 15, source="巡检"),
            report("R3", "LH-01", 9, 35, source="热线"),
            report("R4", "LH-02", 9, 5, source="巡检"),
            report("R5", "LH-02", 9, 40, source="热线"),
        ]

    def test_rerun_produces_identical_order_numbers(self):
        first = serialize(merge_reports(self._rows()))
        second = serialize(merge_reports(self._rows()))
        self.assertEqual(first, second)
        order_nos = [order["order_no"] for order in first["orders"]]
        self.assertEqual(len(order_nos), len(set(order_nos)))

    def test_serialization_is_json_ready_and_traceable(self):
        payload = serialize(merge_reports(self._rows()))
        for order in payload["orders"]:
            self.assertEqual(len(order["report_refs"]), order["report_count"])
            for item in order["reports"]:
                self.assertIn(item["report_ref"], order["report_refs"])
                self.assertTrue(item["occurred_at"])


class ValidationTests(unittest.TestCase):
    def test_duplicate_report_ref_rejected(self):
        with self.assertRaises(ValueError):
            merge_reports([report("R1", "LH-01", 9), report("R1", "LH-02", 10)])

    def test_unknown_judgment_ref_rejected(self):
        with self.assertRaises(ValueError):
            merge_reports([report("R1", "LH-01", 9)],
                          [{"left_ref": "R1", "right_ref": "R9", "relation": "same"}])

    def test_bad_relation_rejected(self):
        with self.assertRaises(ValueError):
            merge_reports([report("R1", "LH-01", 9), report("R2", "LH-01", 9, 5)],
                          [{"left_ref": "R1", "right_ref": "R2", "relation": "maybe"}])

    def test_empty_input_rejected(self):
        with self.assertRaises(ValueError):
            merge_reports([])


if __name__ == "__main__":
    unittest.main(verbosity=2)
