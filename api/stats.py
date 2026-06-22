"""Запись событий обработки OMR в БД. Никогда не роняет основную обработку."""

import logging

from api.db import SessionLocal
from api.stats_models import ProcessingEvent

logger = logging.getLogger(__name__)


def record_processing(
    *,
    task_id: str | None,
    kind: str,
    preset: str | None,
    status: str,
    files_total: int = 0,
    files_completed: int = 0,
    files_failed: int = 0,
    enhance: bool = False,
    analyze: bool = False,
) -> None:
    """Сохранить событие обработки. Ошибки БД глушим — статистика не должна ломать OMR."""
    try:
        db = SessionLocal()
        try:
            db.add(
                ProcessingEvent(
                    task_id=task_id,
                    kind=kind,
                    preset=preset,
                    status=status,
                    files_total=files_total,
                    files_completed=files_completed,
                    files_failed=files_failed,
                    enhance=enhance,
                    analyze=analyze,
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("failed to record processing event for task %s", task_id)
