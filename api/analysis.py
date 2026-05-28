"""Опциональная пост-обработка MusicXML-выхода Audiveris через music21.

Две независимые возможности, включаются флагами в запросе:
  * needFix  — починить файл (Audiveris иногда отдаёт невалидный MusicXML, напр.
               <beam> на ноте-члене аккорда — на таком verovio падает с segfault при
               сборке MIDI). Чиним прогоном через music21, который пересобирает модель.
  * analyze  — собрать метаданные партитуры (тональность, размер, темп, инструменты…)
               и вернуть их дополнительным полем.

music21 импортируется лениво внутри функций — он тяжёлый, и если флаги не заданы,
платить за импорт не нужно. Любая ошибка пост-обработки не должна ронять задачу:
функции деградируют к «ничего не сделали» и возвращают исходный файл.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Нота-член аккорда (<chord/>), на которой висит <beam>. Именно это Audiveris
# иногда генерит, и именно на этом verovio падает при рендере в MIDI.
_NOTE_RE = re.compile(r"<note\b.*?</note>", re.S)


def _read_musicxml_text(path: Path) -> str:
    """Прочитать XML-текст партитуры. Понимает и .musicxml, и сжатый .mxl (zip)."""
    try:
        if path.suffix.lower() == ".mxl":
            with zipfile.ZipFile(path) as archive:
                names = [
                    n for n in archive.namelist()
                    if n.lower().endswith(".xml") and not n.startswith("META-INF")
                ]
                if not names:
                    return ""
                return archive.read(names[0]).decode("utf-8", errors="ignore")
        return path.read_text(errors="ignore")
    except Exception:
        logger.exception("Failed to read MusicXML text from %s", path)
        return ""


def _count_beam_on_chord(text: str) -> int:
    """Сколько нот несут одновременно <chord/> и <beam> — паттерн краша verovio."""
    return sum(1 for note in _NOTE_RE.findall(text) if "<chord/>" in note and "<beam" in note)


def needs_fix(path: Path) -> bool:
    """Эвристика «надо ли чинить»: есть ли в файле структуры, которые ломают
    нижестоящие инструменты (verovio → MIDI). Дёшево: чистый текстовый скан,
    music21 не нужен."""
    return _count_beam_on_chord(_read_musicxml_text(path)) > 0


def _analyze_score(score) -> dict:
    """Достать метаданные из уже разобранной music21-партитуры."""
    from music21 import instrument, meter, tempo as m21tempo

    try:
        key = score.analyze("key")
    except Exception:
        key = None

    time_signatures = [
        ts.ratioString for ts in score.recurse().getElementsByClass(meter.TimeSignature)
    ]
    tempos = [
        m.number
        for m in score.recurse().getElementsByClass(m21tempo.MetronomeMark)
        if m.number is not None
    ]

    parts = list(score.parts)
    instruments: list[str] = []
    for part in parts:
        try:
            inst = part.getInstrument(returnDefault=True)
            if inst is not None and inst.instrumentName:
                instruments.append(inst.instrumentName)
        except Exception:
            pass

    measures = len(parts[0].getElementsByClass("Measure")) if parts else 0

    return {
        "key": str(key) if key is not None else None,
        "key_confidence": (
            round(float(key.correlationCoefficient), 3)
            if key is not None and hasattr(key, "correlationCoefficient")
            else None
        ),
        "time_signatures": time_signatures,
        "tempos": [float(t) for t in tempos],
        "parts": len(parts),
        "instruments": instruments,
        "measures": measures,
        "notes": len(score.recurse().notes),
    }


def repair(path: Path, out_dir: Path | None = None) -> Path | None:
    """Пересобрать MusicXML прогоном через music21 (parse -> write).

    Это убирает невалидные структуры (напр. <beam> на ноте-члене аккорда), на
    которых verovio падает при сборке MIDI. Возвращает путь к починенному
    .musicxml или None, если music21 не смог разобрать/записать файл.
    """
    from music21 import converter

    try:
        score = converter.parse(str(path))
    except Exception:
        logger.exception("music21 failed to parse %s; cannot repair", path)
        return None

    target_dir = out_dir if out_dir is not None else path.parent
    fixed_path = target_dir / f"{path.stem}_fixed.musicxml"
    try:
        score.write("musicxml", fp=str(fixed_path))
    except Exception:
        logger.exception("music21 failed to write fixed file for %s", path)
        return None
    return fixed_path


def postprocess(
    path: Path, analyze: bool = False, need_fix: bool = False
) -> tuple[Path, bool, dict | None]:
    """Применить опциональную music21-пост-обработку к выходному файлу.

    Возвращает (итоговый_путь, был_ли_починен, метаданные_или_None).
    Файл парсится не больше одного раза; при need_fix чиним только если детект
    нашёл проблему. Любой сбой music21 → возврат исходного пути без падения.
    """
    target = path
    fixed = False
    analysis: dict | None = None

    do_fix = need_fix and needs_fix(path)
    if not (analyze or do_fix):
        return target, fixed, analysis

    from music21 import converter

    try:
        score = converter.parse(str(path))
    except Exception:
        logger.exception("music21 failed to parse %s; skipping post-processing", path)
        return target, fixed, analysis

    if do_fix:
        try:
            fixed_path = path.with_name(f"{path.stem}_fixed.musicxml")
            score.write("musicxml", fp=str(fixed_path))
            target, fixed = fixed_path, True
        except Exception:
            logger.exception("music21 failed to write fixed file for %s", path)

    if analyze:
        try:
            analysis = _analyze_score(score)
        except Exception:
            logger.exception("music21 failed to analyze %s", path)

    return target, fixed, analysis
