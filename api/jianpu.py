"""Цзянпу (简谱): пресет `jianpu` уводит задачу в движок jpeditor.

Цифровую нотацию не читает ни homr (ищет нотные станы и на цифрах падает или
выдаёт мусор), ни Audiveris. Поэтому пресет `jianpu` идёт мимо обоих — прямо в
`omr.jianpu.recognize` (движок: omr/engines/jianpu_engine.py). Провал там —
честная ошибка без отката: «успешная» партитура из нот, которых на листе нет,
хуже ошибки.

Плейлист — те же страницы одним запуском движка со склейкой в одну песню.
"""

from __future__ import annotations

import logging
from pathlib import Path

from api.config import settings
from api.exceptions import ProcessingError
from api.presets import Preset

logger = logging.getLogger(__name__)


def is_jianpu_preset(preset: str | None) -> bool:
    return preset == Preset.jianpu.value


def run(input_paths: list[Path], output_dir: Path, stem: str) -> tuple[Path, Path]:
    """Распознать входы одной песней. Возвращает (MusicXML, лог)."""
    from omr.engines.homr_engine import EngineError
    from omr.jianpu.recognize import recognize

    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / f"{stem}.jianpu.log"
    try:
        result = recognize(input_paths, output_dir, stem=stem,
                           timeout=max(settings.jianpu_timeout_seconds, 1),
                           max_pages=settings.max_pdf_pages)
    except EngineError as exc:
        log_path.write_text(f"движок цзянпу: {exc}\n", encoding="utf-8")
        # Хвост «, см. <путь>» — путь на сервере, клиенту он ни к чему.
        raise ProcessingError(f"Цзянпу не распознан: {str(exc).split(', см.')[0]}",
                              log_path=log_path) from exc
    except Exception as exc:
        log_path.write_text(f"распознавание цзянпу упало: {type(exc).__name__}: {exc}\n",
                            encoding="utf-8")
        raise ProcessingError(f"Распознавание цзянпу упало: {exc}", log_path=log_path) from exc

    lines = [f"вход: {', '.join(path.name for path in input_paths)}", "", result.report()]
    if result.engine_log is not None and result.engine_log.exists():
        lines += ["", result.engine_log.read_text(encoding="utf-8")]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if result.musicxml is None:
        reasons = [result.refused] if result.refused else \
            list(dict.fromkeys(page.error for page in result.pages if page.error))
        raise ProcessingError("Цзянпу не распознан: " + ("; ".join(reasons[:3]) or "нет страниц"),
                              log_path=log_path)
    logger.info("jianpu: %s -> %s (страниц %d, распознано %d)", input_paths[0].name,
                result.musicxml.name, len(result.pages), result.recognised)
    return result.musicxml, log_path
