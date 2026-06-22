"""Модель статистики использования основного OMR-API.

Одна строка = одна обработка (single или playlist). По created_at агрегируем
метрики по дням (см. api/stats_routes.py). Пишется воркером после каждой задачи
(см. api/stats.py) — отдельно от эфемерных задач в Redis, чтобы статистика жила
постоянно в Postgres.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class ProcessingEvent(Base):
    __tablename__ = "processing_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # single | playlist
    preset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)  # completed | error
    files_total: Mapped[int] = mapped_column(Integer, default=0)
    files_completed: Mapped[int] = mapped_column(Integer, default=0)
    files_failed: Mapped[int] = mapped_column(Integer, default=0)
    enhance: Mapped[bool] = mapped_column(Boolean, default=False)
    analyze: Mapped[bool] = mapped_column(Boolean, default=False)

    def __str__(self) -> str:
        return f"{self.kind}/{self.status} @ {self.created_at}"
