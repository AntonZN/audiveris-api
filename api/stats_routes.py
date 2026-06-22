"""Метрики использования OMR-API: обработки по дням."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_api_key
from api.db import get_db
from api.models import ApiModel
from api.stats_models import ProcessingEvent

router = APIRouter(prefix="/stats", tags=["Stats"], dependencies=[Depends(get_api_key)])


class DailyStat(ApiModel):
    date: str
    tasks: int = 0          # всего обработок (задач) за день
    completed: int = 0      # успешных
    failed: int = 0         # с ошибкой
    files: int = 0          # успешно обработано файлов


class StatsSummary(ApiModel):
    total_tasks: int = 0
    total_completed: int = 0
    total_failed: int = 0
    days: list[DailyStat]


@router.get(
    "/daily",
    response_model=StatsSummary,
    summary="Обработки по дням",
    description="Сколько обработок было по дням за последние `days` суток (по умолчанию 30).",
)
def daily_stats(
    days: int = Query(30, ge=1, le=365, description="За сколько последних дней"),
    db: Session = Depends(get_db),
) -> StatsSummary:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(
            ProcessingEvent.created_at,
            ProcessingEvent.status,
            ProcessingEvent.files_completed,
        ).where(ProcessingEvent.created_at >= cutoff)
    ).all()

    # Группируем по дате в Python — БД-агностично (без date_trunc/cast).
    buckets: dict[str, dict[str, int]] = {}
    for created_at, status, files in rows:
        key = created_at.date().isoformat()
        b = buckets.setdefault(key, {"tasks": 0, "completed": 0, "failed": 0, "files": 0})
        b["tasks"] += 1
        if status == "completed":
            b["completed"] += 1
        elif status == "error":
            b["failed"] += 1
        b["files"] += files or 0

    items = [
        DailyStat(date=key, tasks=b["tasks"], completed=b["completed"], failed=b["failed"], files=b["files"])
        for key, b in sorted(buckets.items(), reverse=True)
    ]
    return StatsSummary(
        total_tasks=sum(i.tasks for i in items),
        total_completed=sum(i.completed for i in items),
        total_failed=sum(i.failed for i in items),
        days=items,
    )
