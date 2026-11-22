"""原始报障归并的应用服务：指纹、版本化、原子切换与幂等。"""

import hashlib
import json

from sqlalchemy import select

from src.domain.consolidation import (
    legacy_pair_weights,
    normalize_reports,
    plan_publication,
    switch_active,
)
from src.models.consolidation import ConsolidationVersion, Judgment
from src.services.audit import record
from src.utils.time import now


def _fingerprint(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def judgments_fingerprint(judgments):
    """人工判定指纹：排序后哈希，保证判定集合相同则指纹相同（与录入顺序无关）。"""
    normalized = sorted(
        (item.left_ref, item.right_ref, item.relation, item.note or "")
        for item in judgments
    )
    return _fingerprint(normalized)


def extract_raw_reports(rows):
    """从数据批次行中取出原始报障字典（归并模块的输入形态）。"""
    return [
        {
            "report_ref": row["report_ref"],
            "site_id": row["site_id"],
            "source": row["source"],
            "occurred_at": row["occurred_at"],
            "legacy_order": row.get("legacy_order"),
        }
        for row in rows
        if row.get("record_type") == "原始报障"
    ]


def _judgment_dicts(judgments):
    return [
        {"left_ref": item.left_ref, "right_ref": item.right_ref,
         "relation": item.relation, "note": item.note or ""}
        for item in judgments
    ]


def active_version(db, dataset_id):
    """返回数据集当前生效的归并版本，没有则 None。"""
    return db.scalar(
        select(ConsolidationVersion)
        .where(ConsolidationVersion.dataset_id == dataset_id,
               ConsolidationVersion.is_active.is_(True))
    )


def list_versions(db, dataset_id):
    return db.scalars(
        select(ConsolidationVersion)
        .where(ConsolidationVersion.dataset_id == dataset_id)
        .order_by(ConsolidationVersion.id.desc())
        .limit(100)
    ).all()


def consolidate(db, dataset, actor):
    """对一个数据批次执行归并并发布新版本。

    决策（幂等、矛盾、切换）由领域纯函数 :func:`plan_publication` 给出，
    本函数只负责持久化与审计，保证：
      - 相同输入指纹复用已有版本，不重复生成工单；
      - 矛盾时不写新版本、不切换，旧生效版本保留，仅记录审计；
      - 成功时在同一事务内把旧版本下线、新版本上线（一次性切换）。

    返回 ``(版本对象, 是否新建, 统计或矛盾字典)``；矛盾时 action 为 ``conflict``，
    统计字段为矛盾列表。
    """
    raw_rows = extract_raw_reports(dataset.rows)
    if not raw_rows:
        raise ValueError("该数据批次没有原始报障行，无法归并工单。")

    judgments = db.scalars(
        select(Judgment).where(Judgment.dataset_id == dataset.id).order_by(Judgment.id)
    ).all()
    judgment_dicts = _judgment_dicts(judgments)
    judgment_fp = judgments_fingerprint(judgments)
    input_fp = _fingerprint({"reports": raw_rows, "judgments": judgment_dicts})

    existing_versions = db.scalars(
        select(ConsolidationVersion)
        .where(ConsolidationVersion.dataset_id == dataset.id)
        .order_by(ConsolidationVersion.id)
    ).all()
    version_views = [
        {"version_no": item.version_no, "input_fingerprint": item.input_fingerprint,
         "is_active": item.is_active}
        for item in existing_versions
    ]

    action, payload, next_no, statistics = plan_publication(
        raw_rows, judgment_dicts, version_views, input_fp,
        pair_weight=legacy_pair_weights(normalize_reports(raw_rows)),
    )

    if action == "reuse":
        twin = next(item for item in existing_versions if item.input_fingerprint == input_fp)
        return twin, False, None

    if action == "conflict":
        record(db, actor, "consolidation.conflict", dataset.id, contradictions=payload)
        return None, False, {"action": "conflict", "contradictions": payload}

    version = ConsolidationVersion(
        dataset_id=dataset.id,
        version_no=next_no,
        input_fingerprint=input_fp,
        judgment_fingerprint=judgment_fp,
        is_active=False,
        status="active",
        payload=payload,
        author=actor,
    )
    db.add(version)
    db.flush()

    # 原子切换：旧生效版本全部下线，新版本上线，集中度下一次测算即读取新版。
    active_flags = switch_active(version_views, next_no)
    for old, is_active in zip(existing_versions, active_flags):
        old.is_active = is_active
        old.status = "active" if is_active else "superseded"
    version.is_active = True
    version.activated_at = now()
    db.flush()

    record(db, actor, "consolidation.published", version.id,
           dataset_id=dataset.id, version_no=next_no,
           order_count=statistics["order_count"],
           preserved_pairs=statistics["preserved_pairs"])
    return version, True, statistics


def upsert_judgment(db, dataset_id, left_ref, right_ref, relation, note, actor):
    """新增或覆盖一条人工判定；同一报障对只保留最新结论。"""
    left_ref, right_ref = sorted((left_ref.strip(), right_ref.strip()))
    existing = db.scalar(
        select(Judgment).where(
            Judgment.dataset_id == dataset_id,
            Judgment.left_ref == left_ref,
            Judgment.right_ref == right_ref,
        ).with_for_update()
    )
    if existing:
        existing.relation = relation
        existing.note = note or None
        existing.author = actor
        judgment = existing
        created = False
    else:
        judgment = Judgment(
            dataset_id=dataset_id, left_ref=left_ref, right_ref=right_ref,
            relation=relation, note=note or None, author=actor,
        )
        db.add(judgment)
        db.flush()
        created = True
    record(db, actor, f"judgment.{'created' if created else 'updated'}", judgment.id,
           dataset_id=dataset_id, left_ref=left_ref, right_ref=right_ref, relation=relation)
    return judgment, created


def remove_judgment(db, dataset_id, judgment_id, actor):
    item = db.get(Judgment, judgment_id, with_for_update=True)
    if not item or item.dataset_id != dataset_id:
        return False
    db.delete(item)
    record(db, actor, "judgment.removed", judgment_id, dataset_id=dataset_id)
    return True
