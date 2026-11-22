"""原始报障归并、人工判定与版本查询接口。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.consolidation import Judgment
from src.models.identity import User
from src.models.source import Dataset
from src.schemas.requests import JudgmentRequest
from src.services import consolidation as service
from src.utils import views

router = APIRouter(prefix='/consolidation')


def _require_dataset(db, dataset_id):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, '找不到指定的数据批次')
    return dataset


@router.get('/datasets/{dataset_id}/versions')
def versions(dataset_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    _require_dataset(db, dataset_id)
    items = service.list_versions(db, dataset_id)
    return [views.consolidation_version(item) for item in items]


@router.get('/datasets/{dataset_id}/active')
def active(dataset_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    _require_dataset(db, dataset_id)
    item = service.active_version(db, dataset_id)
    return views.consolidation_version(item, detail=True) if item else None


@router.post('/datasets/{dataset_id}/run', status_code=201)
def run(dataset_id: int, db: Session = Depends(connection),
        actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    dataset = _require_dataset(db, dataset_id)
    version, created, outcome = service.consolidate(db, dataset, actor.username)
    if outcome and outcome.get("action") == "conflict":
        # 无解：审计随事务提交，但不写新版本、不切换，旧生效版本保留。
        db.commit()
        db.expire_all()
        active_item = service.active_version(db, dataset_id)
        raise HTTPException(422, {
            'message': '人工判定存在矛盾，无法生成工单，已保留当前生效版本。',
            'contradictions': outcome["contradictions"],
            'active_version': views.consolidation_version(active_item) if active_item else None,
        })
    db.commit()
    return {**views.consolidation_version(version, detail=True), 'created': created}


@router.get('/datasets/{dataset_id}/judgments')
def list_judgments(dataset_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    _require_dataset(db, dataset_id)
    items = db.scalars(
        select(Judgment).where(Judgment.dataset_id == dataset_id).order_by(Judgment.id.desc()).limit(500)
    ).all()
    return [views.judgment(item) for item in items]


@router.post('/datasets/{dataset_id}/judgments', status_code=201)
def add_judgment(dataset_id: int, body: JudgmentRequest, db: Session = Depends(connection),
                 actor: User = Depends(allow(Role.ADMIN))):
    _require_dataset(db, dataset_id)
    if body.relation not in ('same', 'separate'):
        raise ValueError('人工判定关系须为 same（同一故障）或 separate（不同故障）。')
    if body.left_ref.strip() == body.right_ref.strip():
        raise ValueError('不能对同一条报障建立判定。')
    item, created = service.upsert_judgment(
        db, dataset_id, body.left_ref, body.right_ref, body.relation, body.note, actor.username)
    db.commit()
    return {**views.judgment(item), 'created': created}


@router.post('/datasets/{dataset_id}/judgments/{judgment_id}/remove')
def delete_judgment(dataset_id: int, judgment_id: int, db: Session = Depends(connection),
                    actor: User = Depends(allow(Role.ADMIN))):
    _require_dataset(db, dataset_id)
    if not service.remove_judgment(db, dataset_id, judgment_id, actor.username):
        raise HTTPException(404, '找不到该人工判定')
    db.commit()
    return {'removed': True}
