"""分析记录的 ORM 模型。"""
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Analysis(Base):
    """一次 JSON Schema 兼容性分析的完整快照。"""

    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    old_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    new_schema: Mapped[dict] = mapped_column(JSON, nullable=False)
    samples: Mapped[list | None] = mapped_column(JSON, nullable=True)
    report: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # 与分析记录一对一关联的画像快照（级联删除分析时一并删除画像）
    profile: Mapped["Profile | None"] = relationship(
        back_populates="analysis", cascade="all, delete-orphan", uselist=False
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "old_schema": self.old_schema,
            "new_schema": self.new_schema,
            "samples": self.samples or [],
            "report": self.report,
        }


class Profile(Base):
    """文档画像快照：用已保存的新版 schema 批量校验真实文档的结果。

    每次 ``POST /api/analyses/{id}/profile`` 覆盖同一分析的唯二快照
    （``analysis_id`` 唯一），与分析记录关联持久化。
    """

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    valid: Mapped[int] = mapped_column(Integer, nullable=False)
    invalid: Mapped[int] = mapped_column(Integer, nullable=False)
    compliance_rate: Mapped[float] = mapped_column(Float, nullable=False)
    failures: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    analysis: Mapped["Analysis"] = relationship(back_populates="profile")

    def to_dict(self) -> dict:
        return {
            "analysis_id": self.analysis_id,
            "total": self.total,
            "valid": self.valid,
            "invalid": self.invalid,
            "compliance_rate": self.compliance_rate,
            "failures": self.failures or [],
            "created_at": self.created_at.isoformat(),
        }
