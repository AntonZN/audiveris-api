"""Опциональная пост-обработка MusicXML-выхода Audiveris через music21.

Две независимые возможности, включаются флагами в запросе:
  * needFix  — починить файл. Audiveris регулярно отдаёт невалидный MusicXML (напр.
               <beam> на ноте-члене аккорда или несбалансированные <slur>), на котором
               verovio в мобильных сборках падает при renderToMIDI. Чиним БЕЗУСЛОВНО
               прогоном через music21, который пересобирает модель и выкидывает дефекты
               (детект не используем — он не покрывал все падающие случаи).
  * analyze  — собрать метаданные партитуры (тональность, размер, темп, инструменты…)
               и вернуть их дополнительным полем.

Перед любой записью music21 ещё и вырезает текстовый шум (слова, слоги, аккордовые
символы, титулы, рехерсал-метки) — главное оставить темп, остальное всегда убирается
(см. _strip_text). Это решено архитектурно: распознанный Audiveris текст часто мусор,
ломает рендер в мобильных приложениях и потребителям не нужен.

music21 импортируется лениво внутри функций — он тяжёлый, и если флаги не заданы,
платить за импорт не нужно. Любая ошибка пост-обработки не должна ронять задачу:
функции деградируют к «ничего не сделали» и возвращают исходный файл.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)


_XML_DROP_TAGS = frozenset(
    {
        "movement-title",
        "movement-number",
        "work",
        "identification",
        "credit",
    }
)

_XML_BLANK_TAGS = frozenset(
    {
        "part-name",
        "part-abbreviation",
        "part-name-display",
        "part-abbreviation-display",
        "instrument-name",
        "instrument-abbreviation",
    }
)


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


def _strip_text(score) -> None:
    """Вырезать текстовый «мусор», оставив музыку и темп. Действует in-place.

    Убираем: <words> (TextExpression), <lyric>, аккордовые символы (C, Am…),
    титульные тексты (TextBox), рехерсал-метки. Сохраняем: MetronomeMark (BPM),
    TimeSignature, KeySignature, ноты/аккорды/паузы, лиги/штрихи/орнаменты,
    динамику — это музыкальная сущность, не текст. Любой сбой music21 для одного
    типа не валит остальные.
    """
    from music21 import expressions, harmony

    def _drop(cls) -> None:
        try:
            for elem in list(score.recurse().getElementsByClass(cls)):
                site = elem.activeSite
                if site is not None:
                    site.remove(elem)
        except Exception:
            logger.exception("strip: failed to remove %s", cls)

    _drop(expressions.TextExpression)
    _drop(expressions.RehearsalMark)
    _drop(harmony.ChordSymbol)
    _drop("TextBox")
    try:
        for n in score.recurse().getElementsByClass("GeneralNote"):
            if n.lyrics:
                n.lyrics = []
    except Exception:
        logger.exception("strip: failed to clear lyrics")

    # Вычистить метаданные (заголовок, композитор и т.п.), иначе music21 запишет
    # их обратно в <movement-title>/<identification><creator>.
    try:
        from music21 import metadata as m21metadata

        score.metadata = m21metadata.Metadata()
    except Exception:
        logger.exception("strip: failed to reset score metadata")

    # Затереть имена партий и инструментов: их рисуют как подписи у нотоносцев.
    try:
        for part in getattr(score, "parts", []) or []:
            part.partName = None
            part.partAbbreviation = None
            try:
                for inst in part.recurse().getElementsByClass("Instrument"):
                    inst.instrumentName = None
                    inst.instrumentAbbreviation = None
            except Exception:
                logger.exception("strip: failed to clear instrument names")
    except Exception:
        logger.exception("strip: failed to clear part names")


def _strip_text_xml(path: Path) -> None:
    """Финальная XML-зачистка после записи music21. Действует in-place.

    Слой нужен потому, что music21 при записи всё равно вставляет:
      * <identification><creator>Music21</creator><software>…</software></identification>
        (вместе с <miscellaneous-field name="source-file">…</miscellaneous-field> от
        Audiveris — утечка внутреннего пути сервера наружу),
      * <movement-title>...</movement-title> (если был ранее),
      * <part-name>/<instrument-name>/<part-abbreviation>/<instrument-abbreviation>.
    Удаляем оптональные блоки целиком, обязательные по схеме (<part-name>) — обнуляем.
    Сбой — оставляем файл как есть, не падаем.
    """
    try:
        tree = ET.parse(str(path))
    except Exception:
        logger.exception("xml strip: failed to parse %s", path)
        return

    root = tree.getroot()
    try:
        for parent in list(root.iter()):
            for child in list(parent):
                if child.tag in _XML_DROP_TAGS:
                    parent.remove(child)
                elif child.tag in _XML_BLANK_TAGS:
                    child.text = ""
                    for sub in list(child):
                        child.remove(sub)
        tree.write(str(path), encoding="utf-8", xml_declaration=True)
    except Exception:
        logger.exception("xml strip: failed to write %s", path)


def repair(path: Path, out_dir: Path | None = None) -> Path | None:
    """Пересобрать MusicXML прогоном через music21 (parse -> strip text -> write).

    Это убирает невалидные структуры (напр. <beam> на ноте-члене аккорда), на
    которых verovio падает при сборке MIDI, и заодно вырезает текстовый шум
    (см. _strip_text). Возвращает путь к починенному .musicxml или None,
    если music21 не смог разобрать/записать файл.
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
        _strip_text(score)
        score.write("musicxml", fp=str(fixed_path))
    except Exception:
        logger.exception("music21 failed to write fixed file for %s", path)
        return None
    _strip_text_xml(fixed_path)
    return fixed_path


def postprocess(
    path: Path, analyze: bool = False, need_fix: bool = False
) -> tuple[Path, bool, dict | None]:
    """Применить опциональную music21-пост-обработку к выходному файлу.

    Возвращает (итоговый_путь, был_ли_починен, метаданные_или_None).
    Файл парсится не больше одного раза; при need_fix чиним всегда (безусловный
    music21-round-trip). Любой сбой music21 → возврат исходного пути без падения.
    """
    target = path
    fixed = False
    analysis: dict | None = None

    if not (analyze or need_fix):
        return target, fixed, analysis

    from music21 import converter

    try:
        score = converter.parse(str(path))
    except Exception:
        logger.exception("music21 failed to parse %s; skipping post-processing", path)
        return target, fixed, analysis

    if need_fix:
        try:
            _strip_text(score)
            fixed_path = path.with_name(f"{path.stem}_fixed.musicxml")
            score.write("musicxml", fp=str(fixed_path))
            _strip_text_xml(fixed_path)
            target, fixed = fixed_path, True
        except Exception:
            logger.exception("music21 failed to write fixed file for %s", path)

    if analyze:
        try:
            analysis = _analyze_score(score)
        except Exception:
            logger.exception("music21 failed to analyze %s", path)

    return target, fixed, analysis
