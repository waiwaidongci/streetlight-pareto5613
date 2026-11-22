from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class ConsolidationVersion(Base):
    """一次原始报障归并的版本化结果。

    - 同一数据批次 + 同一人工判定集合（指纹）重跑命中唯一约束，不重复生成。
    - ``is_active`` 标记当前生效版本；发布在单事务内切换，旧版本保留可回溯。
    - 归并矛盾时不写新版本，仅记录失败审计，旧 ``is_active`` 版本继续生效。
    """

    __tablename__ = 'consolidation_versions'
    __table_args__ = (
        UniqueConstraint('dataset_id', 'input_fingerprint', name='uq_consolidation_input'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), index=True)
    version_no: Mapped[int]
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(default=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default='active')
    payload: Mapped[dict] = mapped_column(JSON)
    judgment_fingerprint: Mapped[str | None] = mapped_column(String(64))
    contradictions: Mapped[list | None] = mapped_column(JSON)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    activated_at: Mapped[datetime | None]


class Judgment(Base):
    """人工对两条原始报障关系的认定：same 同一故障、separate 不同故障。"""

    __tablename__ = 'manual_judgments'
    __table_args__ = (
        UniqueConstraint('dataset_id', 'left_ref', 'right_ref', name='uq_judgment_pair'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), index=True)
    left_ref: Mapped[str] = mapped_column(String(64))
    right_ref: Mapped[str] = mapped_column(String(64))
    relation: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(500))
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
