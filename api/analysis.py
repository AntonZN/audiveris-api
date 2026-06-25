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
# Название (movement-title / work / movement-number) НЕ удаляется — мобила
# использует его как имя песни. Композитор/encoding/path leaks — внутри
# <identification>, его сносим целиком.
_XML_DROP_TAGS = frozenset(
    {
        # шапка
        "identification",  # <creator>, <software>, <source>, <miscellaneous> → утечки путей
        "credit",  # визуальные подписи на странице (название дублируется в movement-title)
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

    # Сбросить метаданные, но сохранить НАЗВАНИЕ (movementName/title) —
    # мобила использует его как имя песни. Композитор, copyright, encoder и пр.
    # обычно либо мусор от OCR ("(mo-1827)"), либо авто-стэмп music21 ("Music21")
    # — выкидываем.
    try:
        from music21 import metadata as m21metadata

        old = score.metadata
        preserved_movement = getattr(old, "movementName", None) if old is not None else None
        preserved_title = getattr(old, "title", None) if old is not None else None
        score.metadata = m21metadata.Metadata()
        if preserved_movement:
            score.metadata.movementName = preserved_movement
        if preserved_title:
            score.metadata.title = preserved_title
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


def _read_musicxml_root(path: Path) -> "ET.Element | None":
    """Прочитать корневой <score-partwise>/<score-timewise> из .mxl или .xml.

    Для .mxl распаковывает zip и парсит rootfile (через container.xml или
    первый подходящий .xml/.musicxml вне META-INF). Возвращает None, если
    не получилось разобрать.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".mxl":
            with zipfile.ZipFile(path, "r") as zin:
                entries = {zi.filename: zin.read(zi.filename) for zi in zin.infolist()}
            rootfile_name: str | None = None
            container = entries.get("META-INF/container.xml")
            if container:
                try:
                    cr = ET.fromstring(container)
                    for el in cr.iter():
                        tag = el.tag.split("}", 1)[-1]
                        if tag == "rootfile" and el.attrib.get("full-path"):
                            rootfile_name = el.attrib["full-path"]
                            break
                except Exception:
                    pass
            if rootfile_name is None:
                for name in entries:
                    low = name.lower()
                    if (low.endswith(".xml") or low.endswith(".musicxml")) \
                            and not name.startswith("META-INF"):
                        rootfile_name = name
                        break
            if rootfile_name is None or rootfile_name not in entries:
                return None
            return ET.fromstring(entries[rootfile_name])
        return ET.parse(str(path)).getroot()
    except Exception:
        logger.exception("read MusicXML root failed for %s", path)
        return None


def collect_bpm(path: Path) -> int | None:
    """Достать BPM прямо из MusicXML (без music21). Работает на .mxl и .xml.

    Источники в порядке предпочтения:
      1) <sound tempo="N"/>          — самое надёжное, Audiveris всегда дублирует
                                       сюда числовое значение, даже если оно из
                                       словесной ремарки.
      2) <metronome><per-minute>N    — нотный метроном (точка с цифрой).

    Берём первое попавшееся валидное число и **округляем до ближайшего целого** —
    Audiveris из-за внутренних float-преобразований иногда отдаёт «59.99999999»
    или «72.00000001», мобиле такое показывать незачем. Если темпов нет
    вообще — возвращаем None (Audiveris часто не находит BPM на скриншотах низкого
    разрешения или партитурах с только словесными ремарками типа «Adagio»).
    """
    root = _read_musicxml_root(path)
    if root is None:
        return None
    for sound in root.iter("sound"):
        val = sound.get("tempo")
        if val:
            try:
                return round(float(val))
            except ValueError:
                continue
    for pm in root.iter("per-minute"):
        if pm.text:
            try:
                return round(float(pm.text.strip()))
            except ValueError:
                continue
    return None


def _find_mxl_rootfile(entries: "list[tuple[zipfile.ZipInfo, bytes]]") -> str | None:
    """Имя rootfile внутри .mxl: через META-INF/container.xml, иначе первый
    .xml/.musicxml вне META-INF."""
    for zi, data in entries:
        if zi.filename == "META-INF/container.xml":
            try:
                container = ET.fromstring(data)
                for el in container.iter():
                    if el.tag.split("}", 1)[-1] == "rootfile" and el.attrib.get("full-path"):
                        return el.attrib["full-path"]
            except Exception:
                logger.exception("mxl: failed to parse container.xml")
            break
    for zi, _ in entries:
        low = zi.filename.lower()
        if (low.endswith(".xml") or low.endswith(".musicxml")) and not zi.filename.startswith("META-INF"):
            return zi.filename
    return None


def _add_tempo_to_root(root, bpm: int) -> bool:
    """In-place: вставить темп (<metronome> + <sound tempo>) в начало первого
    такта первой партии. False, если структуры нет или темп уже присутствует
    (идемпотентно — не дублируем)."""
    if root.find(".//sound[@tempo]") is not None or root.find(".//per-minute") is not None:
        return False
    part = root.find("part")
    if part is None:
        return False
    measure = part.find("measure")
    if measure is None:
        return False

    direction = ET.Element("direction", {"placement": "above"})
    dtype = ET.SubElement(direction, "direction-type")
    metronome = ET.SubElement(dtype, "metronome")
    ET.SubElement(metronome, "beat-unit").text = "quarter"
    ET.SubElement(metronome, "per-minute").text = str(bpm)
    ET.SubElement(direction, "sound", {"tempo": str(bpm)})

    # По схеме MusicXML <direction> идёт после <attributes>, если он есть в такте.
    insert_at = 0
    for i, child in enumerate(list(measure)):
        if child.tag == "attributes":
            insert_at = i + 1
            break
    measure.insert(insert_at, direction)
    return True


def inject_bpm(path: Path, bpm: int) -> bool:
    """Вписать темп в MusicXML: `<metronome>` + `<sound tempo="N"/>` в первый такт.

    Нужно, когда движок (homr) темп не распознал, а мы достали BPM иначе (через
    Audiveris): значение попадает и в отдаваемый файл (его читает плеер), и снова
    становится доступно через `collect_bpm`. Работает на .mxl и .musicxml/.xml.
    Идемпотентно. Возвращает True, если файл изменён.
    """
    if path.suffix.lower() == ".mxl":
        return _inject_bpm_mxl(path, bpm)
    return _inject_bpm_plain(path, bpm)


def _inject_bpm_plain(path: Path, bpm: int) -> bool:
    try:
        tree = ET.parse(str(path))
    except Exception:
        logger.exception("bpm inject: failed to parse %s", path)
        return False
    if not _add_tempo_to_root(tree.getroot(), bpm):
        return False
    try:
        tree.write(str(path), encoding="utf-8", xml_declaration=True)
        return True
    except Exception:
        logger.exception("bpm inject: failed to write %s", path)
        return False


def _inject_bpm_mxl(path: Path, bpm: int) -> bool:
    """Распаковать .mxl, добавить темп в rootfile, перепаковать (если изменили)."""
    try:
        with zipfile.ZipFile(path, "r") as zin:
            entries = [(zi, zin.read(zi.filename)) for zi in zin.infolist()]
    except Exception:
        logger.exception("bpm inject: failed to read %s", path)
        return False

    rootfile_name = _find_mxl_rootfile(entries)
    if rootfile_name is None:
        logger.warning("bpm inject: no rootfile found in %s", path)
        return False

    changed = False
    new_entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    for zi, data in entries:
        if zi.filename == rootfile_name:
            try:
                root = ET.fromstring(data)
            except Exception:
                logger.exception("bpm inject: failed to parse %s in %s", rootfile_name, path)
                return False
            if _add_tempo_to_root(root, bpm):
                changed = True
                data = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
        new_entries.append((zi, data))

    if not changed:
        return False
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
            for zi, data in new_entries:
                zout.writestr(zi.filename, data)
        return True
    except Exception:
        logger.exception("bpm inject: failed to write %s", path)
        return False


def collect_texts(path: Path) -> dict:
    """Собрать ВСЕ текстовые сущности из MusicXML (без стрипа).

    Вызывается ДО `_strip_text_xml` на сыром выходе Audiveris — мобиле возвращаем
    весь распознанный текст в JSON, чтобы она сама рисовала его в нужном месте
    (заголовок песни / поверх плеера / в тайм-кодах), а не в нотном рендерере,
    где она его всё равно не умеет показывать корректно.

    Возвращает структуру (значения None / [] если ничего не нашли):
      title             — <movement-title> или <work><work-title>
      composer          — <identification><creator type="composer">
      credits           — список <credit-words> (визуальные подписи на странице)
      directions        — список {text, measure, placement} для <words>
      rehearsals        — список {text, measure} для <rehearsal>
      lyrics            — список {text, measure} для <lyric><text>
      part_names        — список <part-name> (имена партий)
      instrument_names  — список <instrument-name>
    """
    empty = {
        "title": None,
        "composer": None,
        "credits": [],
        "directions": [],
        "rehearsals": [],
        "lyrics": [],
        "part_names": [],
        "instrument_names": [],
    }
    root = _read_musicxml_root(path)
    if root is None:
        return empty

    def _txt(el) -> str | None:
        if el is None or el.text is None:
            return None
        s = el.text.strip()
        return s or None

    out = dict(empty)

    # --- шапка ---
    out["title"] = _txt(root.find("movement-title"))
    if not out["title"]:
        work = root.find("work")
        if work is not None:
            out["title"] = _txt(work.find("work-title"))

    for cr in root.findall("identification/creator"):
        if cr.get("type") == "composer":
            t = _txt(cr)
            if t:
                out["composer"] = t
                break

    out["credits"] = [
        t for cw in root.findall(".//credit-words") if (t := _txt(cw))
    ]

    # --- подписи партий / инструментов ---
    out["part_names"] = [
        t for pn in root.findall(".//part-name") if (t := _txt(pn))
    ]
    out["instrument_names"] = [
        t for nm in root.findall(".//instrument-name") if (t := _txt(nm))
    ]

    # --- текст в теле партитуры (с привязкой к такту) ---
    directions: list[dict] = []
    rehearsals: list[dict] = []
    lyrics: list[dict] = []
    for part in root.findall("part"):
        for measure in part.findall("measure"):
            mnum = measure.get("number")
            for d in measure.findall("direction"):
                placement = d.get("placement")
                for w in d.findall("direction-type/words"):
                    t = _txt(w)
                    if t:
                        directions.append({"text": t, "measure": mnum, "placement": placement})
                for r in d.findall("direction-type/rehearsal"):
                    t = _txt(r)
                    if t:
                        rehearsals.append({"text": t, "measure": mnum})
            for note in measure.findall("note"):
                for lyr in note.findall("lyric"):
                    # <lyric><text>...</text>[<elision/><text>...</text>...]</lyric>
                    parts = [t for tx in lyr.findall("text") if (t := _txt(tx))]
                    if parts:
                        lyrics.append({"text": " ".join(parts), "measure": mnum})
    out["directions"] = directions
    out["rehearsals"] = rehearsals
    out["lyrics"] = lyrics
    return out


def _sanitize_divisions(root) -> None:
    """Заменить <divisions>0</divisions> (или пустое/нечисловое) на 480.

    Audiveris изредка выплёвывает <divisions>0</divisions>, на котором music21
    при парсинге валится с `ZeroDivisionError` внутри `xmlToDuration`
    (`noteDivisions / divisions`) — без шанса перехватить выше уровня одной
    конкретной ноты. По смыслу MusicXML 0 не имеет интерпретации (количество
    делений четверти не может быть нулём), поэтому замена на 480 (один из
    стандартных значений) безопасна: длительности нот в этой партии будут
    выражены в той же шкале относительно друг друга.
    """
    DEFAULT = 480
    for div in root.iter("divisions"):
        text = (div.text or "").strip()
        try:
            val = int(text)
        except ValueError:
            val = 0
        if val <= 0:
            logger.warning(
                "MusicXML <divisions>=%r — replacing with %d for music21 safety",
                text, DEFAULT,
            )
            div.text = str(DEFAULT)


def _sanitize_clefs(root) -> None:
    """Зажать <clef><line>N</line></clef> в диапазон 1..5.

    music21 при парсинге зовёт `clefFromString(sign + line)` и для line > 5 или
    < 1 кидает `ClefException` без шанса перехватить выше — вся партия мрёт.
    Audiveris изредка выдаёт `<line>6</line>` на нотоносцах, где у нормальных
    ключей такого положения не бывает (видимо, OMR-косяк с табулатурами или
    шестилинейными нестандартными станами). Безопасный fallback — зажать в [1,5]:
    G2/F4/C3 это и есть скрипичный/басовый/альтовый, ничего ближе не предложишь.
    """
    for clef in root.iter("clef"):
        line_el = clef.find("line")
        if line_el is None or not line_el.text:
            continue
        try:
            n = int(line_el.text.strip())
        except ValueError:
            continue
        if n < 1 or n > 5:
            new = max(1, min(5, n))
            logger.warning(
                "MusicXML <clef><line>=%d outside [1..5]; clamping to %d", n, new,
            )
            line_el.text = str(new)


def _scrub_root(root) -> None:
    """In-place: пройтись по дереву MusicXML, убрать текстовые теги и починить
    структурные дефекты (deviding, clefs), на которых music21 валится при парсе.

    Удаляет целиком всё из _XML_DROP_TAGS, обнуляет содержимое _XML_BLANK_TAGS,
    плюс зовёт защитные правки (см. _sanitize_divisions / _sanitize_clefs).
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
        _sanitize_divisions(root)
        _sanitize_clefs(root)
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


def analyze_only(path: Path) -> dict | None:
    """Посчитать метаданные партитуры БЕЗ music21-фикса (без записи _fixed).

    Тестовый путь: music21 только парсит файл и считает analysis
    (тональность/размеры/темпы/инструменты), но мы НЕ пересобираем модель и
    НЕ пишем `_fixed.musicxml`. Любой сбой music21 → None, задача не падает.
    """
    from music21 import converter

    try:
        score = converter.parse(str(path))
    except Exception:
        logger.exception("music21 failed to parse %s; skipping analysis", path)
        return None

    try:
        return _analyze_score(score)
    except Exception:
        logger.exception("music21 failed to analyze %s", path)
        return None


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
