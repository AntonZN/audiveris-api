"""Постраничная склейка нескольких MusicXML в одну партитуру.

Контекст плейлиста: каждое ФОТО распознаётся homr отдельно и даёт свой MusicXML
на одну страницу. `merge_musicxml` склеивает их в единое произведение —
дописывает такты каждой следующей страницы в соответствующую партию первой,
сквозным образом перенумеровывает такты и выкидывает повторную декларацию
divisions/key/time/clef в первом такте подклеиваемой страницы, если она
совпадает с уже действующей (homr печатает ключ/размер в начале КАЖДОЙ страницы,
а на стыке это читается как ложная смена ключа/размера в середине песни).

Работаем на ``xml.etree.ElementTree`` — как и весь остальной MusicXML-код проекта
(см. analysis.py): он толерантен к неидеальному OMR-выходу и не тянет лишних
зависимостей. MusicXML partwise идёт без XML-namespace, поэтому теги адресуем
напрямую ("part"/"measure"/"attributes"). Чтение .mxl и .musicxml/.xml — через
analysis._read_musicxml_root (распаковывает .mxl, находит rootfile).

Раньше здесь был CLI-инструмент на сторонней библиотеке ``musicxml`` (Alex Gorji);
он переписан в чистую функцию без argparse/glob и без строгого schema-валидатора,
который спотыкался бы на дефектах Audiveris/homr.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from api.analysis import _read_musicxml_root

logger = logging.getLogger(__name__)


def _child_signature(elem: ET.Element) -> str:
    """Подпись элемента по его под-тегам и их тексту (для <key>/<time>).

    <key> → "fifths=-1|mode=major"; <time> → "beats=4|beat-type=4". Атрибуты
    самого элемента (напр. number/print-object) намеренно игнорируем — на какой
    стан он действует, мы уже различаем по @number (см. _staff).
    """
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
    """Номер стана (@number), к которому относится key/time/clef. Без него — '1'.

    У фортепиано один <part> с двумя <clef number="1"/> и <clef number="2"/> —
    поэтому состояние ключей/размеров держим по номеру стана, а не одним значением.
    """
    return elem.get("number", "1")


def _new_state() -> dict:
    """Действующие на текущий момент атрибуты партии."""
    return {"divisions": None, "key": {}, "time": {}, "clef": {}}


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
        for clef in attributes.findall("clef"):
            state["clef"][_staff(clef)] = _clef_signature(clef)


def _strip_redundant(measure: ET.Element, state: dict) -> None:
    """Убрать из первого такта страницы повторную декларацию уже действующих
    divisions/key/time/clef. Пустой после чистки <attributes> удаляем целиком.

    Вызывается ДО _update_state: то, что совпало с действующим — выкидываем
    (оно и так в силе), то, что отличается (реальная смена) — оставляем, и
    _update_state потом подхватит новое значение.
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


def merge_musicxml(input_paths: list[Path], output_path: Path) -> Path:
    """Склеить постранично input_paths (.mxl/.musicxml) в один .musicxml.

    Первый файл — основа (его шапка work/identification остаётся; всё равно потом
    зачистится в _build_success_result). Партии следующих файлов сопоставляются с
    партиями основы ПО ПОРЯДКУ (homr на каждой странице даёт одну и ту же
    структуру партий), их такты дописываются в хвост соответствующей партии с
    непрерывной перенумерацией. Любой нечитаемый файл или отсутствие <part> —
    исключение: молча отдавать песню с дырой нельзя.

    Возвращает output_path.
    """
    paths = [Path(p) for p in input_paths]
    if not paths:
        raise ValueError("merge_musicxml: пустой список файлов")

    base_root = _read_musicxml_root(paths[0])
    if base_root is None:
        raise ValueError(f"merge_musicxml: не прочитать {paths[0]}")
    base_parts = base_root.findall("part")
    if not base_parts:
        raise ValueError(f"merge_musicxml: нет <part> в {paths[0]}")

    # Состояние и счётчик тактов на каждую партию основы — инициализируем,
    # пройдя её собственные такты (последнее объявление атрибута побеждает).
    states = [_new_state() for _ in base_parts]
    counts = []
    for state, part in zip(states, base_parts):
        n = 0
        for measure in part.findall("measure"):
            _update_state(state, measure)
            n += 1
        counts.append(n)

    for path in paths[1:]:
        root = _read_musicxml_root(path)
        if root is None:
            raise ValueError(f"merge_musicxml: не прочитать {path}")
        add_parts = root.findall("part")
        if not add_parts:
            raise ValueError(f"merge_musicxml: нет <part> в {path}")
        if len(add_parts) != len(base_parts):
            logger.warning(
                "merge: разное число партий (%d против %d) в %s; склеиваю по "
                "минимальному индексу",
                len(add_parts), len(base_parts), path,
            )

        for idx, base_part in enumerate(base_parts):
            if idx >= len(add_parts):
                break
            state = states[idx]
            for j, measure in enumerate(add_parts[idx].findall("measure")):
                if j == 0:
                    _strip_redundant(measure, state)
                _update_state(state, measure)
                counts[idx] += 1
                measure.set("number", str(counts[idx]))
                base_part.append(measure)

    output_path = Path(output_path)
    ET.ElementTree(base_root).write(
        str(output_path), encoding="utf-8", xml_declaration=True
    )
    return output_path
