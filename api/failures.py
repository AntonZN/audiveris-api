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
from api.failure_reasons import classify
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


# Логи мелкие (отчёт стадий, stdout движка), но у Audiveris бывают гигантскими,
# а класть в архив мегабайты бессмысленно: причина всегда в первых строках.
_LOG_SUFFIXES = (".omr.txt", ".omr.log", ".engine.log", ".log")
_MAX_LOG_BYTES = 2 * 1024 * 1024
_MAX_LOGS_PER_TASK = 20


def _archive_logs(output_dir: Path | None, dest_dir: Path) -> list[Path]:
    """Скопировать логи обработки в архив. Возвращает копии в порядке полезности.

    Порядок задан `_LOG_SUFFIXES`: сначала отчёт стадий пайплайна (в нём видно,
    что случилось с геометрией и сколько станов нашлось), потом вывод движка.
    Имя копии — путь относительно output_dir через «_», иначе одноимённые логи
    страниц PDF затрут друг друга.
    """
    if not output_dir or not output_dir.exists():
        return []
    found: list[Path] = []
    for suffix in _LOG_SUFFIXES:
        for path in sorted(output_dir.rglob(f"*{suffix}")):
            if path.is_file() and path not in found:
                found.append(path)

    copies: list[Path] = []
    logs_dir = dest_dir / "logs"
    for path in found[:_MAX_LOGS_PER_TASK]:
        try:
            if path.stat().st_size > _MAX_LOG_BYTES:
                continue
            logs_dir.mkdir(parents=True, exist_ok=True)
            flat = "_".join(path.relative_to(output_dir).parts)
            dest = _unique_dest(logs_dir / flat)
            shutil.copyfile(path, dest)
            copies.append(dest)
        except Exception:
            logger.exception("failed to archive log %s", path)
    return copies


def _log_for(filename: str, logs: list[Path]) -> str | None:
    """Лог, относящийся именно к этому входному файлу.

    Логи названы по имени входа (`page.omr.txt`, `page.p02.engine.log`), поэтому
    ищем по основе имени. Не нашли — берём первый: для одиночной задачи он и есть
    нужный, а для плейлиста лучше показать хоть какой-то, чем ничего.
    """
    stem = Path(filename).stem
    for log in logs:
        if log.name.startswith(stem):
            return str(log)
    return str(logs[0]) if logs else None


def record_failure(
    *,
    task_id: str | None,
    kind: str,
    preset: str | None,
    enhance: bool,
    input_paths: list[Path],
    error: str | None,
    output_dir: Path | None = None,
) -> None:
    """Сохранить входные файлы и логи проваленной задачи, завести строки в БД.

    Логи копируем из `output_dir`, потому что там они живут до первой уборки по
    TTL — а разбирают провал обычно позже. Без них в архиве остаётся файл и одна
    строка ошибки: видно ЧТО не получилось, но не видно ПОЧЕМУ.

    Ошибки архивации/БД глушим — аудит провалов не должен ломать сам OMR.
    Вызывать ДО удаления временного input_dir.
    """
    try:
        dest_dir = Path(settings.failures_dir) / (task_id or "unknown")
        dest_dir.mkdir(parents=True, exist_ok=True)

        error_text = (error or "")[: settings.max_error_len] or None
        # Причину считаем ОДИН раз при записи, а не при каждом показе: она нужна
        # для группировки в SQL, а классификатор со временем меняется — пусть в
        # строке останется то, как мы поняли ошибку тогда.
        reason = classify(error)
        logs = _archive_logs(output_dir, dest_dir)

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
                        reason=reason,
                        log_path=_log_for(src.name, logs),
                    )
                )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("failed to record failure for task %s", task_id)
