"""Постраничная склейка нескольких MusicXML в одну партитуру.

Контекст плейлиста: каждое ФОТО распознаётся homr отдельно и даёт свой MusicXML
на одну страницу. `merge_musicxml` склеивает их в единое произведение —
дописывает такты каждой следующей страницы в соответствующую партию, сквозным
образом перенумеровывает такты и выкидывает повторную декларацию
divisions/key/time/clef в первом такте подклеиваемой страницы, если она
совпадает с уже действующей (homr печатает ключ/размер в начале КАЖДОЙ страницы,
а на стыке это читается как ложная смена в середине песни).

ГЛАВНАЯ сложность — homr распознаёт РАЗНОЕ число партий на разных страницах
одной и той же песни (стр.1: мелодия + фортепиано = 2 партии; стр.2: только
фортепиано = 1 партия). Поэтому партии страниц сопоставляются НЕ по порядку, а
по структурной подписи (набор ключей/станов): фортепиано клеится к фортепиано,
а не к мелодии. Партии, которых на странице не оказалось, добиваются
«пустыми» тактами (whole-measure rest), чтобы все партии оставались
синхронны по числу тактов — иначе verovio/music21 рисуют кашу.

Работаем на ``xml.etree.ElementTree`` — как и весь остальной MusicXML-код проекта
(см. analysis.py): он толерантен к неидеальному OMR-выходу и не тянет лишних
зависимостей. MusicXML partwise идёт без XML-namespace, поэтому теги адресуем
напрямую. Чтение .mxl и .musicxml/.xml — через analysis._read_musicxml_root.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from api.analysis import _read_musicxml_root

logger = logging.getLogger(__name__)


# --- подписи элементов (для сравнения «то же самое») -------------------------

def _child_signature(elem: ET.Element) -> str:
    """Подпись по под-тегам и их тексту: <key> → "fifths=-1|mode=major"."""
    return "|".join(f"{c.tag}={(c.text or '').strip()}" for c in elem)


def _clef_signature(clef: ET.Element) -> str:
    """Подпись <clef> по sign/line/clef-octave-change."""
    return "|".join(
        (
            clef.findtext("sign") or "",
            clef.findtext("line") or "",
            clef.findtext("clef-octave-change") or "",
        )
    )


def _staff(elem: ET.Element) -> str:
    """Номер стана (@number) для key/time/clef. Без него — '1'."""
    return elem.get("number", "1")


def _part_signature(part: ET.Element) -> tuple:
    """Структурная подпись партии — отсортированный набор подписей её ключей.

    У фортепиано два ключа (скрипичный+басовый) → подпись из двух элементов, у
    мелодии один → из одного. По ней фортепиано одной страницы сопоставляется
    с фортепиано другой, а не с мелодией. Пустой кортеж (нет ключей вовсе) —
    специальный случай, такие партии матчатся только по индексу.
    """
    return tuple(sorted(_clef_signature(c) for c in part.findall(".//clef")))


# --- действующее состояние партии (divisions/key/time/clef/staves) -----------

def _new_state() -> dict:
    return {
        "divisions": None,
        "key": {},
        "time": {},
        "clef": {},
        "beats": None,
        "beat_type": None,
        "staves": set(),
    }


def _update_state(state: dict, measure: ET.Element) -> None:
    """Обновить «действующие атрибуты» по тому, что объявлено в такте."""
    for attributes in measure.findall("attributes"):
        div = attributes.find("divisions")
        if div is not None and div.text and div.text.strip():
            state["divisions"] = div.text.strip()
        for key in attributes.findall("key"):
            state["key"][_staff(key)] = _child_signature(key)
        for time in attributes.findall("time"):
            state["time"][_staff(time)] = _child_signature(time)
            beats, beat_type = time.findtext("beats"), time.findtext("beat-type")
            if beats and beat_type:
                state["beats"], state["beat_type"] = beats.strip(), beat_type.strip()
        for clef in attributes.findall("clef"):
            state["clef"][_staff(clef)] = _clef_signature(clef)
    for note in measure.findall("note"):
        st = note.findtext("staff")
        if st and st.strip():
            state["staves"].add(st.strip())


def _strip_redundant(measure: ET.Element, state: dict) -> None:
    """Убрать из первого такта страницы повторную декларацию уже действующих
    divisions/key/time/clef. Пустой после чистки <attributes> удаляем целиком.

    Вызывается ДО _update_state: совпавшее с действующим выкидываем (оно в силе),
    отличающееся (реальная смена) оставляем — _update_state подхватит новое.
    """
    for attributes in measure.findall("attributes"):
        div = attributes.find("divisions")
        if div is not None and div.text and div.text.strip() == state["divisions"]:
            attributes.remove(div)
        for key in attributes.findall("key"):
            if state["key"].get(_staff(key)) == _child_signature(key):
                attributes.remove(key)
        for time in attributes.findall("time"):
            if state["time"].get(_staff(time)) == _child_signature(time):
                attributes.remove(time)
        for clef in attributes.findall("clef"):
            if state["clef"].get(_staff(clef)) == _clef_signature(clef):
                attributes.remove(clef)
        if len(attributes) == 0:
            measure.remove(attributes)


# --- пустые такты для добивки отсутствующих партий ---------------------------

def _rest_measure(
    number: int,
    divisions: str | None,
    beats: str | None,
    beat_type: str | None,
    staves: set | list,
) -> ET.Element:
    """Такт из whole-measure rest'ов — по одному на каждый стан партии.

    Нужен, когда на странице нет партии, которая есть в других (homr её не нашёл):
    чтобы все партии оставались равной длины по тактам, добиваем недостающие
    «молчанием». Длительность считаем по текущим divisions и размеру (по
    умолчанию 4/4) — для rest measure="yes" это ориентир, точное заполнение такта.
    """
    div = int(divisions) if divisions and divisions.isdigit() else 4
    b = int(beats) if beats and beats.isdigit() else 4
    bt = int(beat_type) if beat_type and beat_type.isdigit() else 4
    dur = max(1, div * b * 4 // bt)  # divisions/quarter * число четвертей в такте

    staff_list = sorted(staves) if staves else ["1"]
    multi = len(staff_list) > 1
    measure = ET.Element("measure", {"number": str(number)})
    for i, st in enumerate(staff_list):
        if i > 0:
            backup = ET.SubElement(measure, "backup")
            ET.SubElement(backup, "duration").text = str(dur)
        note = ET.SubElement(measure, "note")
        ET.SubElement(note, "rest", {"measure": "yes"})
        ET.SubElement(note, "duration").text = str(dur)
        ET.SubElement(note, "voice").text = str(i + 1)
        if multi:
            ET.SubElement(note, "staff").text = st
    return measure


def _scan_rest_params(part: ET.Element) -> tuple:
    """(divisions, beats, beat_type, staves) для генерации пустых тактов новой
    партии — берём по последнему объявлению в её собственных тактах."""
    state = _new_state()
    for measure in part.findall("measure"):
        _update_state(state, measure)
    return state["divisions"], state["beats"], state["beat_type"], state["staves"]


# --- сопоставление партий страницы с накопленными ----------------------------

def _match_parts(page_parts: list, canon_sigs: list) -> list:
    """Сопоставить партии страницы накопленным партиям: сначала по структурной
    подписи (один-к-одному), потом оставшиеся — по индексу среди свободных.

    Возвращает список (длиной с page_parts): индекс накопленной партии или None
    (None → партии страницы нет соответствия, заведём новую).
    """
    used: set[int] = set()
    result: list[int | None] = [None] * len(page_parts)
    page_sigs = [_part_signature(p) for p in page_parts]

    for i, sig in enumerate(page_sigs):
        if sig == ():
            continue
        for j, csig in enumerate(canon_sigs):
            if j not in used and sig == csig:
                result[i] = j
                used.add(j)
                break

    for i in range(len(page_parts)):
        if result[i] is None:
            for j in range(len(canon_sigs)):
                if j not in used:
                    result[i] = j
                    used.add(j)
                    break
    return result


# --- основная функция --------------------------------------------------------

def merge_musicxml(input_paths: list[Path], output_path: Path) -> Path:
    """Склеить постранично input_paths (.mxl/.musicxml) в один .musicxml.

    Первый файл — основа (порядок чтения + шапка). Партии следующих страниц
    приклеиваются к одноимённым по структуре партиям основы, недостающие партии
    добиваются пустыми тактами, все партии держатся равной длины. Любой
    нечитаемый файл / отсутствие <part> — исключение (песню с дырой не отдаём).

    Возвращает output_path.
    """
    paths = [Path(p) for p in input_paths]
    if not paths:
        raise ValueError("merge_musicxml: пустой список файлов")

    base_root = _read_musicxml_root(paths[0])
    if base_root is None:
        raise ValueError(f"merge_musicxml: не прочитать {paths[0]}")
    canon_parts = base_root.findall("part")
    if not canon_parts:
        raise ValueError(f"merge_musicxml: нет <part> в {paths[0]}")
    part_list = base_root.find("part-list")

    # Состояние/счётчик тактов каждой партии основы.
    states = [_new_state() for _ in canon_parts]
    counts = []
    for state, part in zip(states, canon_parts):
        n = 0
        for measure in part.findall("measure"):
            _update_state(state, measure)
            n += 1
        counts.append(n)

    # Выровнять партии основы между собой (homr мог дать разное число тактов).
    total = max(counts) if counts else 0
    for j, part in enumerate(canon_parts):
        while counts[j] < total:
            counts[j] += 1
            part.append(_rest_measure(
                counts[j], states[j]["divisions"],
                states[j]["beats"], states[j]["beat_type"], states[j]["staves"],
            ))

    for path in paths[1:]:
        root = _read_musicxml_root(path)
        if root is None:
            raise ValueError(f"merge_musicxml: не прочитать {path}")
        page_parts = root.findall("part")
        if not page_parts:
            raise ValueError(f"merge_musicxml: нет <part> в {path}")

        canon_sigs = [_part_signature(p) for p in canon_parts]
        mapping = _match_parts(page_parts, canon_sigs)

        # Сколько тактов добавляет страница (по самой длинной её партии) и какой
        # на ней размер — им добиваем отсутствующие партии.
        span = max((len(p.findall("measure")) for p in page_parts), default=0)
        page_time = None
        for p in page_parts:
            t = p.find(".//time")
            if t is not None and t.findtext("beats") and t.findtext("beat-type"):
                page_time = (t.findtext("beats").strip(), t.findtext("beat-type").strip())
                break

        matched: set[int] = set()
        for i, page_part in enumerate(page_parts):
            j = mapping[i]
            if j is None:
                # Партия, которой ещё не было ни на одной странице: заводим новую,
                # добиваем её пустыми тактами за все прошлые страницы (back-pad).
                j = _add_canonical_part(
                    base_root, part_list, canon_parts, states, counts, page_part, total
                )
                logger.warning(
                    "merge: новая партия (нет на предыдущих страницах) в %s", path
                )
            state = states[j]
            for k, measure in enumerate(page_part.findall("measure")):
                if k == 0:
                    _strip_redundant(measure, state)
                _update_state(state, measure)
                counts[j] += 1
                measure.set("number", str(counts[j]))
                canon_parts[j].append(measure)
            matched.add(j)

        # Все партии должны вырасти ровно на span — добиваем короткие.
        total += span
        for j, part in enumerate(canon_parts):
            beats = page_time[0] if page_time else states[j]["beats"]
            beat_type = page_time[1] if page_time else states[j]["beat_type"]
            while counts[j] < total:
                counts[j] += 1
                part.append(_rest_measure(
                    counts[j], states[j]["divisions"], beats, beat_type,
                    states[j]["staves"],
                ))

    output_path = Path(output_path)
    ET.ElementTree(base_root).write(
        str(output_path), encoding="utf-8", xml_declaration=True
    )
    return output_path


def _add_canonical_part(
    base_root: ET.Element,
    part_list: ET.Element | None,
    canon_parts: list,
    states: list,
    counts: list,
    page_part: ET.Element,
    prior_total: int,
) -> int:
    """Завести новую накопленную партию (встретилась на странице, но не раньше).

    Регистрирует <score-part> в <part-list> и пустой <part> в конце партитуры,
    добивает её prior_total пустыми тактами (чтобы выровняться с остальными), и
    возвращает её индекс. Реальные такты страницы допишет основной цикл.
    """
    existing_ids = {p.get("id") for p in canon_parts}
    pid = page_part.get("id") or "P"
    if pid in existing_ids:
        n = len(canon_parts) + 1
        while f"P{n}" in existing_ids:
            n += 1
        pid = f"P{n}"

    if part_list is not None:
        score_part = ET.SubElement(part_list, "score-part", {"id": pid})
        ET.SubElement(score_part, "part-name").text = ""
    new_part = ET.SubElement(base_root, "part", {"id": pid})

    canon_parts.append(new_part)
    states.append(_new_state())
    counts.append(0)
    idx = len(canon_parts) - 1

    div, beats, beat_type, staves = _scan_rest_params(page_part)
    while counts[idx] < prior_total:
        counts[idx] += 1
        new_part.append(_rest_measure(counts[idx], div, beats, beat_type, staves))
    return idx
