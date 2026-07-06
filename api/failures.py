"""Архивация входных файлов проваленных задач. Никогда не роняет OMR.

Раньше входные файлы задачи безусловно удалялись вместе с временным input_dir
(см. api/worker.py). Теперь для задач, завершившихся ошибкой, мы сначала копируем
их входы в постоянную failures_dir и заводим строку в таблице failed_files —
чтобы позже провести аудит проблемных файлов в админке (вьюха «Проблемные файлы»).
"""

import logging
import shutil
from pathlib import Path

from api.config import settings
from api.db import SessionLocal
from api.failures_models import FailedFile

logger = logging.getLogger(__name__)


def _unique_dest(dest: Path) -> Path:
    """Не перезатирать уже сохранённую копию при коллизии имён (напр. один и тот
    же файл провалился повторно): добавляем суффикс _1, _2, …"""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    i = 1
    while True:
        candidate = dest.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def record_failure(
    *,
    task_id: str | None,
    kind: str,
    preset: str | None,
    enhance: bool,
    input_paths: list[Path],
    error: str | None,
) -> None:
    """Сохранить копии входных файлов проваленной задачи и завести строки в БД.

    Ошибки архивации/БД глушим — аудит провалов не должен ломать сам OMR.
    Вызывать ДО удаления временного input_dir.
    """
    try:
        dest_dir = Path(settings.failures_dir) / (task_id or "unknown")
        dest_dir.mkdir(parents=True, exist_ok=True)

        error_text = (error or "")[: settings.max_error_len] or None

        db = SessionLocal()
        try:
            for src in input_paths:
                stored_path: str | None = None
                try:
                    if src.exists():
                        dest = _unique_dest(dest_dir / src.name)
                        shutil.copyfile(src, dest)
                        stored_path = str(dest)
                except Exception:
                    logger.exception("failed to archive input file %s", src)

                db.add(
                    FailedFile(
                        task_id=task_id,
                        kind=kind,
                        preset=preset,
                        enhance=enhance,
                        filename=src.name,
                        stored_path=stored_path,
                        error=error_text,
                    )
                )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("failed to record failure for task %s", task_id)
