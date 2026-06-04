"""Пост-обработка MusicXML-выхода Audiveris.

Политика: **фиксим всегда**. Audiveris регулярно отдаёт невалидный MusicXML
(<beam> на ноте-члене аккорда, несбалансированные <slur>), на котором verovio
в мобильных сборках падает при renderToMIDI. Прогон через music21 пересобирает
модель и выкидывает дефекты; флаг отключения не предусмотрен — все случаи,
которые мы пробовали детектить статически, не покрывали реальные падения.

Параллельно вычищаем **текстовый шум и утечки путей сервера** (слова, слоги,
аккордовые символы, титулы, рехерсал-метки, <identification> с <source>/
<miscellaneous-field>, имена партий/инструментов). Главное — оставить темп
(<metronome>/<per-minute>/<sound tempo>) и музыкальную фактуру (динамика, лиги,
штрихи). См. _strip_text (на уровне music21-объектов) и _strip_text_xml
(финальный XML-pass, работает и на .mxl, и на .xml).

Опциональный флаг `analyze` добавляет в ответ метаданные партитуры (тональность,
размер, темп, инструменты…) — это единственная развилка в постобработке.

music21 импортируется лениво внутри функций — он тяжёлый. Любая ошибка
не должна ронять задачу: функции деградируют к «вернули исходный файл».
"""

from __future__ import annotations

import io
import logging
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


# Теги, которые удаляются целиком (опциональные по схеме MusicXML).
_XML_DROP_TAGS = frozenset(
    {
        # шапка/титулы
        "movement-title",
        "movement-number",
        "work",
        "identification",  # <creator>, <software>, <source>, <miscellaneous> → утечки путей
        "credit",
        # текст в теле партитуры
        "words",  # <direction-type><words>…</words></direction-type>
        "lyric",  # подписанные слоги под нотами
        "rehearsal",  # рехерсал-метки (буквы A, B…)
        "other-direction",  # произвольный текстовый direction
    }
)

# Теги, обязательные по схеме (минимум 1 раз): обнуляем текст, тег оставляем.
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


def _scrub_root(root) -> None:
    """In-place: пройтись по дереву MusicXML и убрать текстовые теги.

    Удаляет целиком всё из _XML_DROP_TAGS, обнуляет содержимое _XML_BLANK_TAGS.
    Не валится, если структура неожиданная — просто логирует.
    """
    try:
        for parent in list(root.iter()):
            for child in list(parent):
                if child.tag in _XML_DROP_TAGS:
                    parent.remove(child)
                elif child.tag in _XML_BLANK_TAGS:
                    child.text = ""
                    for sub in list(child):
                        child.remove(sub)
    except Exception:
        logger.exception("xml strip: scrub failed")


def _strip_text_xml(path: Path) -> None:
    """Вырезать текстовые теги из MusicXML на месте. Работает на .musicxml/.xml и .mxl.

    Применяется к сырому выходу Audiveris (.mxl) ДО music21-round-trip — нужен
    отдельным слоем, потому что music21 при чтении .mxl и записи .musicxml
    шапку identification переоформляет, но содержательно похожие куски
    (movement-title, <source>/<miscellaneous-field> с утечкой пути) пробрасывает
    обратно. Безопаснее срезать ДО парса, а потом ещё раз после write.

    Для .mxl распаковываем zip, чистим rootfile, перепаковываем обратно.

    Что убираем (см. _XML_DROP_TAGS / _XML_BLANK_TAGS):
      * шапку: <movement-title>, <work>, <identification> (с утечкой пути в
        <source>/<miscellaneous-field>), <credit>;
      * тело: <words> (текстовые direction), <lyric>, <rehearsal>, <other-direction>;
      * подписи нотоносцев: <part-name>, <part-abbreviation>, <instrument-name>,
        <instrument-abbreviation> (обнуляем, тег обязателен по схеме).

    Не трогает: <metronome>/<per-minute>/<sound tempo> (темп — оставляем!),
    <dynamics>/<wedge>/<pedal> (музыкальная фактура, не текст), <lyric-font>
    в <defaults> (декларация шрифта, не контент).
    """
    suffix = path.suffix.lower()
    if suffix == ".mxl":
        _strip_text_mxl(path)
    else:
        _strip_text_xml_plain(path)


def _strip_text_xml_plain(path: Path) -> None:
    try:
        tree = ET.parse(str(path))
    except Exception:
        logger.exception("xml strip: failed to parse %s", path)
        return
    _scrub_root(tree.getroot())
    try:
        tree.write(str(path), encoding="utf-8", xml_declaration=True)
    except Exception:
        logger.exception("xml strip: failed to write %s", path)


def _strip_text_mxl(path: Path) -> None:
    """Распаковать .mxl, почистить rootfile, перепаковать обратно."""
    try:
        with zipfile.ZipFile(path, "r") as zin:
            entries = [(zi, zin.read(zi.filename)) for zi in zin.infolist()]
    except Exception:
        logger.exception("mxl strip: failed to read %s", path)
        return

    # Найти rootfile через META-INF/container.xml; иначе — первый .xml/.musicxml вне META-INF.
    rootfile_name: str | None = None
    for zi, data in entries:
        if zi.filename == "META-INF/container.xml":
            try:
                container = ET.fromstring(data)
                for el in container.iter():
                    tag = el.tag.split("}", 1)[-1]
                    if tag == "rootfile" and el.attrib.get("full-path"):
                        rootfile_name = el.attrib["full-path"]
                        break
            except Exception:
                logger.exception("mxl strip: failed to parse container.xml in %s", path)
            break
    if rootfile_name is None:
        for zi, _ in entries:
            low = zi.filename.lower()
            if (low.endswith(".xml") or low.endswith(".musicxml")) \
                    and not zi.filename.startswith("META-INF"):
                rootfile_name = zi.filename
                break
    if rootfile_name is None:
        logger.warning("mxl strip: no rootfile found in %s", path)
        return

    new_entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    for zi, data in entries:
        if zi.filename == rootfile_name:
            try:
                root = ET.fromstring(data)
            except Exception:
                logger.exception("mxl strip: failed to parse %s in %s", rootfile_name, path)
                return
            _scrub_root(root)
            new_data = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
                root, encoding="utf-8"
            )
            new_entries.append((zi, new_data))
        else:
            new_entries.append((zi, data))

    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
            for zi, data in new_entries:
                # Сохраняем имя/флаги исходной записи, пересчитывая CRC.
                zout.writestr(zi.filename, data)
    except Exception:
        logger.exception("mxl strip: failed to write %s", path)


def _m21_write_musicxml(score, fixed_path: Path) -> bool:
    """Записать score как MusicXML с fallback на makeNotation=False.

    Audiveris на плотных партитурах (триоли, аккорды на нескольких голосах)
    рассыпает нумерацию <voice> между тактами: m3 → {1,5,6}, m4 → {5}, m5 →
    {1,5,6,7}. music21 при экспорте идёт по `makeRests`→`makeTies`, цепляется
    за voice 6 в m3, ищет её в m4 и валится с `KeyError: '6'` в
    `iterator.__getitem__`. Лечится отключением makeNotation: оставшиеся
    структурные правки (beam-on-chord, разбалансированные лиги) делаются на
    парсе/райтере и от этого флага не зависят. На здоровых файлах оба режима
    дают идентичный байт-в-байт результат — поэтому пробуем сначала строгий
    путь, а на сбое тихо откатываемся к толерантному.
    """
    try:
        score.write("musicxml", fp=str(fixed_path))
        return True
    except Exception:
        logger.warning(
            "music21 export crashed (likely Audiveris voice-id mess), "
            "retrying with makeNotation=False for %s",
            fixed_path,
        )
    try:
        score.write("musicxml", fp=str(fixed_path), makeNotation=False)
        return True
    except Exception:
        logger.exception("music21 export failed even with makeNotation=False for %s",
                         fixed_path)
        return False


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
    except Exception:
        logger.exception("strip failed before write for %s", path)
    if not _m21_write_musicxml(score, fixed_path):
        return None
    _strip_text_xml(fixed_path)
    return fixed_path


def postprocess(
    path: Path, analyze: bool = False
) -> tuple[Path, bool, dict | None]:
    """music21-пост-обработка выхода Audiveris. Чиним ВСЕГДА.

    Возвращает (итоговый_путь, был_ли_починен, метаданные_или_None).
    Music21 парсит файл, `_strip_text` вырезает текстовые сущности на уровне
    объектов, `score.write` пишет рядом `_fixed.musicxml`, `_strip_text_xml`
    делает финальный XML-pass (выкидывает остатки, которые music21-writer
    вставляет обратно). При analyze=True ещё считает метаданные. Любой сбой
    music21 → возврат исходного пути без падения.
    """
    target = path
    fixed = False
    analysis: dict | None = None

    from music21 import converter

    try:
        score = converter.parse(str(path))
    except Exception:
        logger.exception("music21 failed to parse %s; skipping post-processing", path)
        return target, fixed, analysis

    try:
        _strip_text(score)
    except Exception:
        logger.exception("strip failed before write for %s", path)
    fixed_path = path.with_name(f"{path.stem}_fixed.musicxml")
    if _m21_write_musicxml(score, fixed_path):
        _strip_text_xml(fixed_path)
        target, fixed = fixed_path, True

    if analyze:
        try:
            analysis = _analyze_score(score)
        except Exception:
            logger.exception("music21 failed to analyze %s", path)

    return target, fixed, analysis
