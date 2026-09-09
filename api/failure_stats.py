"""Сводка провалов по причинам.

Отдельно от `api/admin.py` намеренно: там вьюха, которую нельзя ни запустить, ни
протестировать без sqladmin, а здесь — арифметика, в которой и живут ошибки
(доли, сортировка, пустой период). Вьюха берёт готовые строки и только
раскрашивает их.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from api.db import SessionLocal
from api.failure_reasons import hint, label
from api.failures_models import FailedFile


def by_reason(cutoff: datetime, session_factory=SessionLocal) -> tuple[list[dict], int]:
    """Провалы с `cutoff` по причинам: (строки от частых к редким, всего файлов).

    Считаем по ФАЙЛАМ, а не по задачам: в плейлисте провалиться может один снимок
    из пяти, и интересен именно он.
    """
    db = session_factory()
    try:
        rows = db.execute(
            select(FailedFile.reason, func.count(FailedFile.id))
            .where(FailedFile.created_at >= cutoff)
            .group_by(FailedFile.reason)
        ).all()
    finally:
        db.close()

    total = sum(int(count or 0) for _, count in rows)
    reasons = [
        {
            "code": code or "unknown",
            "label": label(code),
            "hint": hint(code),
            "count": int(count or 0),
            # Доля в процентах — целыми: дробные проценты в такой таблице только
            # мешают, решение принимается по «половина провалов вот из-за этого».
            "share": round(int(count or 0) * 100 / total) if total else 0,
        }
        for code, count in rows
    ]
    reasons.sort(key=lambda item: (-item["count"], item["label"]))
    return reasons, total
