"""Врезка пакета `omr/` в пайплайн API.

Тонкая прослойка: `omr` ничего не знает про API, API не знает про внутренности
`omr`. Здесь только перевод одного в другое — путь на входе, `(MusicXML, лог)` на
выходе, ошибки в `ProcessingError`.

Что даёт замена прямого вызова homr на этот путь (замерено на архиве провалов и
на архиве успешных задач, см. omr/README.md):

* **поворот на 90°** — пять файлов, где движок падал или выдавал 1-2 такта
  мусора, стали давать 21-49 тактов. Скан, положенный в сканер боком, — самый
  частый одиночный класс отказа в архиве;
* **разрез разворота книги** — `spread-01` 261 -> 480 нот, `book-03` 268 -> 377,
  `book-02` 183 -> 330;
* **PDF постранично** — `pdf-02` через Audiveris не давал НИЧЕГО (одна страница
  с копирайтом обнуляла экспорт всей книги), через `omr` даёт 102 такта. На
  трёх PDF из архива успешных задач новый путь и быстрее Audiveris, и находит
  больше нот (1300 -> 1582, 902 -> 1107, 498 -> 753);
* **защита от краха на одном стане** (в раннере `omr/engines/_homr_runner.py`) —
  `book-01`, `book-02` и `phone-01` падали целиком, теперь доходят до выхода.

На файлах, которые и так работали, путь нейтрален: 10 из 10 задач архива
успешных дают результат, среднее число нот совпадает.
"""

from __future__ import annotations

import logging
from pathlib import Path

from api.config import settings
from api.exceptions import ProcessingError

logger = logging.getLogger(__name__)

# Растр, который умеет читать пайплайн. PDF проверяется отдельно по сигнатуре:
# расширение файла нам не подконтрольно.
_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
                    ".heic", ".heif"}


def is_supported(path: Path) -> bool:
    """Возьмётся ли `omr` за этот файл: любой растр или PDF."""
    from omr.stages.pdf import is_pdf

    return path.suffix.lower() in _RASTER_SUFFIXES or is_pdf(path)


def run(input_path: Path, output_dir: Path) -> tuple[Path, Path | None]:
    """Распознать файл пайплайном `omr`. Возвращает (MusicXML, лог).

    Отчёт пайплайна пишется в лог целиком: по нему видно, какие стадии
    сработали и почему — без этого разбирать провал в проде нечем.
    """
    from omr.config import DEFAULT
    from omr.recognize import recognize

    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{input_path.stem}.omr.log"

    try:
        result = recognize(
            input_path, output_dir, DEFAULT,
            timeout=max(settings.homr_timeout_seconds, 1),
            max_pages=settings.max_pdf_pages,
        )
    except Exception as exc:
        log_path.write_text(f"omr упал: {type(exc).__name__}: {exc}\n")
        raise ProcessingError(
            f"Пайплайн omr не смог обработать файл: {exc}", log_path=log_path
        ) from exc

    lines = [f"вход: {input_path.name}", ""]
    for page in result.pages:
        if page.prepare is not None:
            lines.append(page.prepare.report())
    lines += ["", result.report()]

    if result.musicxml is None:
        log_path.write_text("\n".join(lines))
        raise ProcessingError(
            "Пайплайн omr не дал MusicXML: " + _failure_reason(result),
            log_path=log_path,
        )

    lines += ["", _apply_tempo(result.musicxml, result.ocr_texts)]
    log_path.write_text("\n".join(lines))

    logger.info(
        "omr: %s -> %s (страниц %d, распознано %d)",
        input_path.name, result.musicxml.name, len(result.pages), result.recognised,
    )
    return result.musicxml, log_path


def _failure_reason(result) -> str:
    """Короткая причина провала для КЛИЕНТА (уходит в поле error ответа).

    Постранично причина повторяется, а хвост «, см. <путь>» — это путь на
    сервере: в ответ API такое отдавать незачем. Подробности целиком остаются в
    логе, ссылка на который идёт рядом.
    """
    reasons: list[str] = []
    # Замечание о масштабе — впереди: если интервал между линейками вне рабочего
    # коридора, движок на этом кадре не заработает, и «движок не справился» тут
    # называет следствие вместо причины. Замерено на боевом файле: крупный план
    # одного стана, 186px на интервал, homr падает.
    for page in result.pages:
        for stage in (page.prepare.stages if page.prepare else []):
            if stage.name == "normalize" and "слишком" in stage.detail:
                if stage.detail not in reasons:
                    reasons.append(stage.detail)
    for page in result.pages:
        reason = (page.error or page.skipped).split(", см.")[0].strip()
        if reason and reason not in reasons:
            reasons.append(reason)
    return "; ".join(reasons[:3]) or "нет страниц с нотами"


def _apply_tempo(musicxml: Path, ocr_texts: list[str]) -> str:
    """Вписать в результат темп, прочитанный движком и им же выброшенный.

    homr темп в MusicXML не пишет никогда, но метрономную строку («♩=95») он
    OCR-ит над верхним станом и отбрасывает как «не заголовок» — раннер эти
    строки перехватывает. Разбираем их здесь: если получилось, `collect_bpm`
    дальше найдёт темп сам, и дорогая проба через Audiveris не запустится.

    Возвращает строку для лога — по ней видно, что именно прочиталось.
    """
    from api.analysis import bpm_from_texts, collect_bpm, inject_bpm

    if not ocr_texts:
        return "темп: движок не отбросил ни одной строки OCR"

    shown = "; ".join(ocr_texts[:5])
    try:
        if collect_bpm(musicxml) is not None:
            return f"темп: уже в выходе движка; OCR-строки: {shown}"
        bpm = bpm_from_texts(ocr_texts)
        if bpm is None:
            return f"темп: в OCR-строках числа не нашлось ({shown})"
        if not inject_bpm(musicxml, bpm):
            return f"темп: {bpm} прочитан, но вписать в файл не удалось ({shown})"
        return f"темп: {bpm} — из OCR-строк движка ({shown})"
    except Exception as exc:  # темп не обязателен, задачу из-за него не роняем
        logger.exception("не смог разобрать темп из строк OCR: %s", ocr_texts)
        return f"темп: разбор упал ({type(exc).__name__}: {exc})"
