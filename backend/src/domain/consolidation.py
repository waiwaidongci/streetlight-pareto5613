"""原始报障归并为工单的领域逻辑。

口径（与需求一一对应）：

1. 一张工单只允许包含同一灯组的原始报障；不同灯组天然分开。
2. 同一张工单内，最早报障时间与最晚报障时间相差不超过 30 分钟（含 30 分钟）。
3. 人工认定「同一故障」的若干报障必须同单（必连约束）；人工认定「不同故障」
   的两条报障必须分单（必分约束）。
4. 优化目标按字典序：先让工单总数最少；工单数相同时，尽量保留上一版中
   本来同单的报障对（保留对数最多）。
5. 必连与必分约束、或必连与半小时窗口矛盾时无解，抛出 :class:`MergeConflict`，
   其中携带全部矛盾记录，调用方据此保留旧版。

本模块只依赖 Python 标准库，便于在没有第三方包的环境下单测。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

WINDOW_MINUTES = 30
SECONDS_PER_MINUTE = 60
# 单灯组搜索节点上限；超过后保留贪心可行解，避免异常数据导致长尾耗时。
MAX_SEARCH_NODES = 200_000
# 单灯组参与精确分支定界的最大单元数；超出后改用确定性贪心。
# 集合划分本身为 NP 难，实际业务中同一灯组、同一时段的报障数量远小于此值。
EXACT_UNIT_LIMIT = 24


class MergeConflict(ValueError):
    """归并无解：人工判定与窗口规则或彼此之间存在矛盾。"""

    def __init__(self, message, contradictions=None):
        super().__init__(message)
        self.contradictions = contradictions or []


# --------------------------------------------------------------------------- #
# 并查集：人工认定同一故障（必连）
# --------------------------------------------------------------------------- #
class _UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))
        self.weight = [1] * size

    def find(self, node):
        while self.parent[node] != node:
            self.parent[node] = self.parent[self.parent[node]]
            node = self.parent[node]
        return node

    def union(self, left, right):
        root_a, root_b = self.find(left), self.find(right)
        if root_a == root_b:
            return
        if self.weight[root_a] < self.weight[root_b]:
            root_a, root_b = root_b, root_a
        self.parent[root_b] = root_a
        self.weight[root_a] += self.weight[root_b]


@dataclass(frozen=True)
class Judgment:
    """一条人工判定。

    relation 为 ``same`` 表示两条报障属同一故障（必须同单），
    为 ``separate`` 表示属不同故障（必须分单）。
    """

    left_ref: str
    right_ref: str
    relation: str
    note: str = ""


@dataclass
class Report:
    ref: str
    site_id: str
    source: str
    occurred_at: datetime
    legacy_order: str | None = None
    row_no: int | None = None


@dataclass
class Order:
    order_no: str
    site_id: str
    reports: list[Report] = field(default_factory=list)

    @property
    def span_minutes(self):
        stamps = sorted(item.occurred_at for item in self.reports)
        return int((stamps[-1] - stamps[0]).total_seconds() // SECONDS_PER_MINUTE)

    @property
    def first_at(self):
        return min(item.occurred_at for item in self.reports)

    @property
    def last_at(self):
        return max(item.occurred_at for item in self.reports)


def _parse_time(value, label):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
    raise ValueError(f"{label}须使用有效的 YYYY-MM-DD HH:MM 报障时间。")


def _normalize_site(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空。")
    return value.strip().upper()


def normalize_reports(raw_rows):
    """把外部字典行规范成 :class:`Report`，完成基础校验。"""
    if not isinstance(raw_rows, (list, tuple)) or not raw_rows:
        raise ValueError("原始报障为空，无法归并工单。")
    reports = []
    seen = set()
    for index, row in enumerate(raw_rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条报障记录格式有误。")
        ref = row.get("report_ref")
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"第 {index} 条报障缺少报障编号。")
        ref = ref.strip()
        if ref in seen:
            raise ValueError(f"报障编号「{ref}」重复。")
        seen.add(ref)
        legacy = row.get("legacy_order")
        if legacy is not None and isinstance(legacy, str) and not legacy.strip():
            legacy = None
        reports.append(
            Report(
                ref=ref,
                site_id=_normalize_site(row.get("site_id"), f"报障「{ref}」的灯组编号"),
                source=str(row.get("source") or "").strip() or "未注明",
                occurred_at=_parse_time(row.get("occurred_at"), f"报障「{ref}」的报障时间"),
                legacy_order=legacy,
                row_no=index,
            )
        )
    return reports


def normalize_judgments(raw_judgments, valid_refs):
    """规范人工判定并检查编号是否存在、关系是否合法。"""
    judgments = []
    for index, item in enumerate(raw_judgments or [], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 条人工判定格式有误。")
        left = str(item.get("left_ref") or "").strip()
        right = str(item.get("right_ref") or "").strip()
        relation = str(item.get("relation") or "").strip().lower()
        note = str(item.get("note") or "").strip()
        if not left or not right:
            raise ValueError(f"第 {index} 条人工判定须填写两条报障编号。")
        if left == right:
            raise ValueError(f"第 {index} 条人工判定不能针对同一条报障「{left}」。")
        if relation not in ("same", "separate"):
            raise ValueError(f"第 {index} 条人工判定的关系须为 same 或 separate。")
        missing = [ref for ref in (left, right) if ref not in valid_refs]
        if missing:
            raise ValueError(f"人工判定引用了不存在的报障编号：{'、'.join(missing)}。")
        judgments.append(Judgment(left, right, relation, note))
    return judgments


# --------------------------------------------------------------------------- #
# 单灯组最优划分（分支定界）
# --------------------------------------------------------------------------- #
def _greedy_partition(size, compatible, weights):
    """按时间顺序贪心构造可行初解，返回 (组数, 分组, 保留对分数)。"""
    groups: list[list[int]] = []
    score = 0
    for idx in range(size):
        best_pos, best_gain = None, None
        for pos, group in enumerate(groups):
            if all(compatible[idx][member] for member in group):
                value = sum(weights[idx][member] for member in group)
                if best_gain is None or value > best_gain:
                    best_pos, best_gain = pos, value
        if best_pos is None:
            groups.append([idx])
        else:
            score += best_gain
            groups[best_pos].append(idx)
    return len(groups), groups, score


def _optimal_partition(units, cannot_links, pair_weight):
    """对同一灯组的单元做字典序最优划分。

    分两阶段精确求解：
      阶段一在所有可行划分中求最少组数（纯窗口贪心下界 + 人工必分团下界 + 对称性破除）；
      阶段二把组数锁定为该最小值，最大化保留的旧版同单报障对分数。

    :param units: 单元元组 ``(最早时间, 最晚时间, 报障引用元组)``，按最早时间排序。
    :param cannot_links: 单元下标之间的必分邻接集合。
    :param pair_weight: 报障有序对的保留权重，键为 ``(ref_a, ref_b)``。
    :returns: ``(组数, 每组单元下标列表, 保留报障对总数)``。
    """
    size = len(units)
    if size == 1:
        return 1, [[0]], 0

    compatible = [[True] * size for _ in range(size)]
    weights = [[0] * size for _ in range(size)]
    window_span = WINDOW_MINUTES * SECONDS_PER_MINUTE
    for i in range(size):
        for j in range(i + 1, size):
            for ref_a in units[i][2]:
                for ref_b in units[j][2]:
                    weights[i][j] += pair_weight.get((ref_a, ref_b), 0)
            weights[j][i] = weights[i][j]
            if j in cannot_links[i] or (units[j][0] - units[i][0]).total_seconds() > window_span:
                compatible[i][j] = compatible[j][i] = False

    manual_mask = [0] * size
    for i in range(size):
        manual_mask[i] = sum((1 << j) for j in range(size) if j in cannot_links[i])

    def window_lower(cursor):
        """仅受时间窗约束时，剩余单元所需的最少组数（贪心扫描的最优解）。

        按最早时间依次把单元并入任意一个跨度仍不超过 30 分钟的已有组，否则
        开新组。区间划分下该贪心给出最少组数；人工必分只会增加组数，因此这是
        完整问题的合法下界。
        """
        group_ends = []
        for idx in range(cursor, size):
            start = units[idx][0]
            for pos, end in enumerate(group_ends):
                if (start - end).total_seconds() <= window_span:
                    group_ends[pos] = start  # 组内成员按时间有序，更新该组当前最晚起点
                    break
            else:
                group_ends.append(start)
        return len(group_ends)

    def clique_lower(cursor):
        """人工必分图的贪心极大团规模（团内两两被认定为不同故障，必须各自分单）。

        只统计人工必分边；时间窗造成的不兼容不在此列，避免高估下界。
        """
        candidates = list(range(cursor, size))
        count = 0
        while candidates:
            pivot = candidates[0]
            count += 1
            bit = 1 << pivot
            candidates = [idx for idx in candidates[1:] if manual_mask[idx] & bit]
        return count

    exact = size <= EXACT_UNIT_LIMIT
    greedy_count, greedy_groups, greedy_score = _greedy_partition(size, compatible, weights)

    if not exact:
        # NP 难实例的安全回退：贪心仍保证窗口与必分约束可行、结果确定可复现。
        return greedy_count, greedy_groups, greedy_score

    # ---- 阶段一：求最少组数（忽略分数，只判断可行性）----
    upper = greedy_count
    minimum = upper
    feasibility_nodes = 0

    def feasible_with_limit(cursor, groups, limit):
        nonlocal feasibility_nodes
        feasibility_nodes += 1
        if feasibility_nodes > MAX_SEARCH_NODES:
            return False
        if cursor == size:
            return True
        # 注意：剩余单元仍可并入已存在的组，不能用「已用组数 + 剩余下界」剪枝。
        for group in groups:
            if all(compatible[cursor][member] for member in group):
                group.append(cursor)
                if feasible_with_limit(cursor + 1, groups, limit):
                    group.pop()
                    return True
                group.pop()
        if len(groups) < limit:
            groups.append([cursor])
            if feasible_with_limit(cursor + 1, groups, limit):
                return True
            groups.pop()
        return False

    global_lower = max(window_lower(0), clique_lower(0))
    for limit in range(global_lower, upper):
        feasibility_nodes = 0
        if feasible_with_limit(0, [], limit):
            minimum = limit
            break

    # ---- 阶段二：组数固定为 minimum，最大化保留分 ----
    best_groups = greedy_groups if greedy_count == minimum else None
    best_score = greedy_score if greedy_count == minimum else -1

    def optimistic_bound(cursor):
        return sum(weights[idx][other]
                   for idx in range(cursor, size)
                   for other in range(idx)
                   if compatible[idx][other])

    score_nodes = 0

    def maximize(cursor, groups, score):
        nonlocal best_groups, best_score, score_nodes
        score_nodes += 1
        if score_nodes > MAX_SEARCH_NODES:
            return
        if cursor == size:
            if len(groups) == minimum and score > best_score:
                best_score = score
                best_groups = [list(group) for group in groups]
            return
        if len(groups) == minimum and score + optimistic_bound(cursor) <= best_score:
            return
        options = []
        for pos, group in enumerate(groups):
            if all(compatible[cursor][member] for member in group):
                options.append((sum(weights[cursor][member] for member in group), pos))
        options.sort(reverse=True)
        for gain, pos in options:
            groups[pos].append(cursor)
            maximize(cursor + 1, groups, score + gain)
            groups[pos].pop()
        if len(groups) < minimum:
            groups.append([cursor])
            maximize(cursor + 1, groups, score)
            groups.pop()

    maximize(0, [], 0)
    if best_groups is None:
        # 节点上限触发且未在固定组数下找到解，回退到可行的贪心划分。
        return greedy_count, greedy_groups, greedy_score
    return minimum, best_groups, best_score


def _build_units(reports, judgments):
    """应用必连判定生成并查集单元，并收集全部矛盾。"""
    index_of = {item.ref: pos for pos, item in enumerate(reports)}
    forest = _UnionFind(len(reports))
    contradictions = []

    same_pairs = [item for item in judgments if item.relation == "same"]
    separate_pairs = [item for item in judgments if item.relation == "separate"]

    for judgment in same_pairs:
        forest.union(index_of[judgment.left_ref], index_of[judgment.right_ref])

    members = defaultdict(list)
    for pos, item in enumerate(reports):
        members[forest.find(pos)].append(item)

    def sorted_pair(a, b):
        return (a, b) if a <= b else (b, a)

    separate_by_roots = defaultdict(list)
    for judgment in separate_pairs:
        root_a = forest.find(index_of[judgment.left_ref])
        root_b = forest.find(index_of[judgment.right_ref])
        if root_a == root_b:
            contradictions.append({
                "type": "same_and_separate",
                "report_refs": sorted([judgment.left_ref, judgment.right_ref]),
                "judgment_note": judgment.note,
                "message": f"报障 {judgment.left_ref} 与 {judgment.right_ref} 既被认定同一故障又被认定不同故障。",
            })
        else:
            separate_by_roots[sorted_pair(root_a, root_b)].append(judgment)

    unit_records = []
    window_span = WINDOW_MINUTES * SECONDS_PER_MINUTE
    for root, items in members.items():
        stamps = [item.occurred_at for item in items]
        if (max(stamps) - min(stamps)).total_seconds() > window_span:
            refs = sorted(item.ref for item in items)
            contradictions.append({
                "type": "same_exceeds_window",
                "report_refs": refs,
                "message": f"人工认定同一故障的报障 {'、'.join(refs)} 时间跨度超过 {WINDOW_MINUTES} 分钟，无法同单。",
            })
        sites = {item.site_id for item in items}
        if len(sites) > 1:
            refs = sorted(item.ref for item in items)
            contradictions.append({
                "type": "same_cross_site",
                "report_refs": refs,
                "message": f"人工认定同一故障的报障 {'、'.join(refs)} 分属不同灯组，工单限定同一灯组。",
            })
        unit_records.append((root, items))

    if contradictions:
        raise MergeConflict("人工判定存在矛盾，无法生成工单。", contradictions)

    return unit_records, separate_by_roots


def merge_reports(raw_rows, raw_judgments=None, pair_weight=None):
    """把原始报障归并成工单。

    :param raw_rows: 原始报障字典列表，字段见 :func:`normalize_reports`。
    :param raw_judgments: 人工判定列表，字段见 :class:`Judgment`。
    :param pair_weight: 上一版同单报障对的权重，缺省每对权重为 1，键为有序对。
    :returns: ``{"orders": 订单列表, "statistics": 统计}``。
    :raises MergeConflict: 约束无解时抛出，矛盾列表附在异常上。
    """
    reports = normalize_reports(raw_rows)
    judgments = normalize_judgments(raw_judgments, {item.ref for item in reports})
    unit_records, separate_by_roots = _build_units(reports, judgments)
    pair_weight = pair_weight or {}

    by_site = defaultdict(list)
    for root, items in unit_records:
        site = items[0].site_id
        earliest = min(item.occurred_at for item in items)
        latest = max(item.occurred_at for item in items)
        refs = tuple(sorted(item.ref for item in items))
        by_site[site].append((earliest, latest, refs, root))

    orders = []
    total_preserved = 0
    for site in sorted(by_site):
        site_units = sorted(by_site[site], key=lambda unit: (unit[0], unit[2]))
        root_to_index = {unit[3]: pos for pos, unit in enumerate(site_units)}
        cannot_links = defaultdict(set)
        for (root_a, root_b) in separate_by_roots:
            if root_a in root_to_index and root_b in root_to_index:
                idx_a, idx_b = root_to_index[root_a], root_to_index[root_b]
                cannot_links[idx_a].add(idx_b)
                cannot_links[idx_b].add(idx_a)
        solver_units = [(unit[0], unit[1], unit[2]) for unit in site_units]
        _, grouped, preserved = _optimal_partition(solver_units, cannot_links, pair_weight)
        total_preserved += preserved
        for group in grouped:
            refs = sorted(ref for unit_index in group for ref in solver_units[unit_index][2])
            selected = [next(item for item in reports if item.ref == ref) for ref in refs]
            selected.sort(key=lambda item: (item.occurred_at, item.ref))
            orders.append(Order(order_no="", site_id=site, reports=selected))

    orders.sort(key=lambda order: (order.site_id, order.first_at))
    site_sequence = defaultdict(int)
    for order in orders:
        site_sequence[order.site_id] += 1
        order.order_no = f"WO-{order.site_id}-{site_sequence[order.site_id]:02d}"

    statistics = {
        "report_count": len(reports),
        "order_count": len(orders),
        "site_count": len(by_site),
        "preserved_pairs": total_preserved,
        "legacy_pair_total": _legacy_pair_count(reports),
        "window_minutes": WINDOW_MINUTES,
    }
    return {"orders": orders, "statistics": statistics}


def _legacy_pair_count(reports):
    grouped = defaultdict(list)
    for item in reports:
        if item.legacy_order:
            grouped[item.legacy_order].append(item.ref)
    return sum(len(refs) * (len(refs) - 1) // 2 for refs in grouped.values())


def legacy_pair_weights(reports):
    """根据上一版工单号生成报障有序对权重表（每对权重为 1）。"""
    grouped = defaultdict(list)
    for item in reports:
        if getattr(item, "legacy_order", None):
            grouped[item.legacy_order].append(item.ref)
    weights = {}
    for refs in grouped.values():
        refs = sorted(refs)
        for i, left in enumerate(refs):
            for right in refs[i + 1:]:
                weights[(left, right)] = 1
    return weights


def plan_publication(raw_rows, raw_judgments, current_versions, input_fingerprint, pair_weight=None):
    """纯函数：计算一次归并发布所需的全部决策，不触碰数据库。

    :param raw_rows: 原始报障字典列表。
    :param raw_judgments: 人工判定字典列表。
    :param current_versions: 该批次已有版本的轻量字典列表，每项至少含
        ``input_fingerprint``、``is_active``、``version_no``。
    :param input_fingerprint: 由服务层对「报障 + 判定」计算的 SHA-256 指纹。
    :returns: 四元组 ``(action, payload, next_version_no, statistics)``：
        - ``reuse``：指纹命中已有版本，payload 为该版本号，不重复生成；
        - ``publish``：需要发布新版，payload 为可持久化结构；
        - ``conflict``：归并矛盾，payload 为矛盾列表，旧版保留。
    """
    twin = next((item for item in current_versions
                 if item.get("input_fingerprint") == input_fingerprint), None)
    if twin is not None:
        return "reuse", {"version_no": twin["version_no"]}, twin["version_no"], None

    reports = normalize_reports(raw_rows)
    weights = pair_weight if pair_weight is not None else legacy_pair_weights(reports)
    try:
        result = merge_reports(raw_rows, raw_judgments, pair_weight=weights)
    except MergeConflict as conflict:
        return "conflict", conflict.contradictions, None, None
    payload = serialize(result)
    next_no = max((item.get("version_no", 0) for item in current_versions), default=0) + 1
    return "publish", payload, next_no, result["statistics"]


def switch_active(versions, new_version_no):
    """纯函数：返回版本生效标记的切换方案。

    新版本置为 active，其余旧版本置为 superseded。输入为版本字典列表，
    返回与输入等长、同序的 ``is_active`` 布尔列表。
    """
    return [item.get("version_no") == new_version_no for item in versions]


def _source_sort_key(source):
    order = {"热线": 0, "巡检": 1}
    return order.get(source, 99)


def serialize(result):
    """把归并结果转成可 JSON 持久化的结构。"""
    orders = []
    for order in result["orders"]:
        ordered_reports = sorted(order.reports, key=lambda item: (item.occurred_at, item.ref))
        orders.append({
            "order_no": order.order_no,
            "site_id": order.site_id,
            "first_at": order.first_at.isoformat(),
            "last_at": order.last_at.isoformat(),
            "span_minutes": order.span_minutes,
            "report_count": len(order.reports),
            "sources": sorted({item.source for item in order.reports}, key=_source_sort_key),
            "report_refs": [item.ref for item in ordered_reports],
            "reports": [
                {
                    "report_ref": item.ref,
                    "site_id": item.site_id,
                    "source": item.source,
                    "occurred_at": item.occurred_at.isoformat(),
                    "legacy_order": item.legacy_order,
                }
                for item in ordered_reports
            ],
        })
    return {"orders": orders, "statistics": result["statistics"]}
