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

# Кадр, который ушёл в движок: `<имя>.clean.png` рядом с выходом. Полутоновый
# PNG шириной 1920 — это сотни килобайт, но битый вход бывает и тяжелее.
_PREPARED_SUFFIXES = (".clean.png",)
_MAX_PREPARED_BYTES = 8 * 1024 * 1024

_MAX_FILES_PER_TASK = 20


def _archive_artefacts(
    output_dir: Path | None,
    dest_dir: Path,
    suffixes: tuple[str, ...],
    subdir: str,
    max_bytes: int,
) -> list[Path]:
    """Скопировать артефакты обработки в архив. Возвращает копии по полезности.

    Порядок задан порядком `suffixes`: у логов первым идёт отчёт стадий (в нём
    видно, что случилось с геометрией и сколько станов нашлось), потом вывод
    движка. Имя копии — путь относительно output_dir через «_», иначе
    одноимённые артефакты страниц PDF затирают друг друга.
    """
    if not output_dir or not output_dir.exists():
        return []
    found: list[Path] = []
    for suffix in suffixes:
        for path in sorted(output_dir.rglob(f"*{suffix}")):
            if path.is_file() and path not in found:
                found.append(path)

    copies: list[Path] = []
    target = dest_dir / subdir
    for path in found[:_MAX_FILES_PER_TASK]:
        try:
            if path.stat().st_size > max_bytes:
                continue
            target.mkdir(parents=True, exist_ok=True)
            flat = "_".join(path.relative_to(output_dir).parts)
            dest = _unique_dest(target / flat)
            shutil.copyfile(path, dest)
            copies.append(dest)
        except Exception:
            logger.exception("failed to archive artefact %s", path)
    return copies


def _artefact_for(filename: str, artefacts: list[Path]) -> str | None:
    """Артефакт, относящийся именно к этому входному файлу.

    Артефакты названы по имени входа (`page.omr.txt`, `page.p02.clean.png`),
    поэтому ищем по основе имени. Не нашли — берём первый: для одиночной задачи
    он и есть нужный, а для плейлиста лучше показать хоть какой-то, чем ничего.
    """
    stem = Path(filename).stem
    for artefact in artefacts:
        if artefact.name.startswith(stem):
            return str(artefact)
    return str(artefacts[0]) if artefacts else None


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

    Логи и подготовленный кадр копируем из `output_dir`, потому что там они живут
    до первой уборки по TTL — а разбирают провал обычно позже. Без них в архиве
    остаётся файл и одна строка ошибки: видно ЧТО не получилось, но не видно
    ПОЧЕМУ и что мы с картинкой успели сделать.

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
        logs = _archive_artefacts(
            output_dir, dest_dir, _LOG_SUFFIXES, "logs", _MAX_LOG_BYTES)
        # Кадр перед отправкой в движок: по нему видно, что подготовка сделала с
        # геометрией. Без него по логу понятно «станов 0», но не понятно почему.
        prepared = _archive_artefacts(
            output_dir, dest_dir, _PREPARED_SUFFIXES, "prepared", _MAX_PREPARED_BYTES)

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
                        log_path=_artefact_for(src.name, logs),
                        prepared_path=_artefact_for(src.name, prepared),
                    )
                )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("failed to record failure for task %s", task_id)
