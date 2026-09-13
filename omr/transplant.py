"""Перенос ремарок из выхода Audiveris в выход homr: динамика, вилки, 8va, педаль.

    python -m omr.transplant homr.musicxml audiveris.mxl [-o out.musicxml]

Зачем. homr читает ноты лучше Audiveris, но динамики, вилок, октавных линий и
педали не выдаёт вовсе — их нет в словаре модели. Audiveris их находит. Замер
на `tests/images/symbols`: вилки cresc/dim — 65-70% полноты при 100% точности,
динамика — 86%/86% с верным типом в 12 из 12, 8va — 1 из 1. Лиги и штрихи у
Audiveris НЕ берём: на Dichterliebe у него 46 ложных лиг на 48 настоящих.

Как переносится одна ремарка:

1. стан Audiveris -> стан homr: по нотам (`refcheck.compare`, Левенштейн по
   высотам). Стан, у которого совпадение ниже половины, не сопоставляется —
   ремарка уйдёт не туда, лучше без неё;
2. такт -> такт: выравнивание по мешкам высот (`symcheck.align_measures`).
   Audiveris и homr теряют такты в разных местах (Debussy: у Audiveris 11 тактов
   из 12), номер такта сравнивать нельзя;
3. место в такте — доля его длины, привязанная к ближайшей ноте того же стана
   (ремарка пишется перед нотой, как её пишут редакторы). Нет ноты рядом —
   ставим по доле, с `<offset>`.

Парные знаки (вилка, 8va, педаль) переносятся только ЦЕЛИКОМ: вилка без конца —
та же беда, что несбалансированная лига, на которой падает verovio в мобильном
клиенте.

8va — не только знак. В MusicXML высота нот звучащая, а оба движка пишут под
линией написанную (Debussy, т.11: у обоих ровно на 12 полутонов ниже эталона).
Поэтому ноты homr под перенесённой линией сдвигаются на октаву. Какие ноты под
линией — решает сам Audiveris: берутся места нот, которые в его выходе стоят
между началом и концом линии на её стане.

Координаты вёрстки (`default-x/y`, `relative-x/y`) не переносятся — они про
страницу Audiveris, а рисует клиент по-своему.
"""

from __future__ import annotations

import argparse
import copy
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from omr.refcheck import compare, load_root
from omr.symcheck import align_measures, read_symbols

KINDS = ("dynamics", "wedge", "octave-shift", "pedal", "repeat")

_LAYOUT_ATTRIBUTES = ("default-x", "default-y", "relative-x", "relative-y")
# Стан ниже этой точности совпадения нот не сопоставляем.
_MIN_STAFF_ACCURACY = 0.5
# Как далеко (доля такта) искать ноту, к которой привязать ремарку.
_SNAP = 0.15
# Допуск (доля такта), с которым нота homr считается «на месте» ноты под 8va.
_OCTAVE_EPS = 0.06


@dataclass
class TransplantReport:
    added: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    shifted_notes: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added)

    def summary(self) -> str:
        if not self.added and not self.skipped:
            return "символы Audiveris: переносить нечего"
        added = ", ".join(f"{kind} {count}" for kind, count in sorted(self.added.items())) or "ничего"
        text = f"символы Audiveris: перенесено {added}"
        if self.shifted_notes:
            text += f"; нот под 8va сдвинуто на октаву: {self.shifted_notes}"
        if self.skipped:
            text += "; пропущено: " + ", ".join(
                f"{reason} {count}" for reason, count in sorted(self.skipped.items()))
        return text


@dataclass
class _Timed:
    element: ET.Element
    onset: float
    staff: int
    chord: bool = False
    grace: bool = False


def _staff_of(element: ET.Element, default: int = 1) -> int:
    text = (element.findtext("staff") or "").strip()
    return int(text) if text.isdigit() else default


def _walk(measure: ET.Element) -> tuple[list[_Timed], float, float]:
    """Элементы такта с моментом времени; длина такта; позиция в конце списка."""
    position = longest = last_onset = 0.0
    items: list[_Timed] = []
    for element in measure:
        tag = element.tag
        if tag == "backup":
            position -= float(element.findtext("duration") or 0)
            items.append(_Timed(element, position, 0))
        elif tag == "forward":
            position += float(element.findtext("duration") or 0)
            longest = max(longest, position)
            items.append(_Timed(element, position, 0))
        elif tag == "note":
            grace = element.find("grace") is not None
            chord = element.find("chord") is not None
            duration = 0.0 if grace else float(element.findtext("duration") or 0)
            if chord:
                onset = last_onset
            else:
                onset = last_onset = position
                position += duration
            longest = max(longest, position)
            items.append(_Timed(element, onset, _staff_of(element), chord, grace))
        elif tag == "direction":
            onset = position + float(element.findtext("offset") or 0)
            items.append(_Timed(element, onset, _staff_of(element, 0)))
        else:
            items.append(_Timed(element, position, 0))
    return items, longest, position


# ----------------------------------------------------------------------------------
# Что есть у Audiveris
# ----------------------------------------------------------------------------------

@dataclass
class _Mark:
    kind: str                 # dynamics | wedge | octave-shift | pedal
    role: str                 # single | start | stop
    number: str
    element: ET.Element       # потомок <direction-type>
    direction: ET.Element
    part: int
    staff: int
    measure: int
    frac: float
    covered: list[tuple[int, float]] = field(default_factory=list)   # места нот под 8va


def _role(kind: str, element: ET.Element) -> str:
    kind_type = element.get("type") or ""
    if kind == "dynamics":
        return "single"
    if kind == "wedge":
        return {"crescendo": "start", "diminuendo": "start", "stop": "stop"}.get(kind_type, "")
    if kind == "octave-shift":
        return {"up": "start", "down": "start", "stop": "stop"}.get(kind_type, "")
    if kind == "pedal":
        return {"start": "start", "stop": "stop", "sostenuto": "start"}.get(kind_type, "")
    return ""


def _collect_marks(root: ET.Element, kinds) -> dict[int, list[_Mark]]:
    marks: dict[int, list[_Mark]] = defaultdict(list)
    for part_index, part in enumerate(root.findall("part")):
        open_octaves: dict[str, _Mark] = {}
        for measure_index, measure in enumerate(part.findall("measure")):
            items, longest, _ = _walk(measure)
            for item in items:
                frac = item.onset / longest if longest > 0 else 0.0
                element = item.element
                if element.tag == "note":
                    if item.grace or element.find("pitch") is None:
                        continue
                    for mark in open_octaves.values():
                        if mark.staff == item.staff:
                            mark.covered.append((measure_index, frac))
                    continue
                if element.tag == "barline":
                    repeat = element.find("repeat")
                    if "repeat" in kinds and repeat is not None \
                            and repeat.get("direction") in ("forward", "backward"):
                        way = repeat.get("direction")
                        marks[part_index].append(_Mark(
                            "repeat", "single", way, repeat, element, part_index, 1,
                            measure_index, 0.0 if way == "forward" else 1.0))
                    continue
                if element.tag != "direction":
                    continue
                staff = item.staff or 1
                for direction_type in element.findall("direction-type"):
                    for child in direction_type:
                        if child.tag not in kinds:
                            continue
                        role = _role(child.tag, child)
                        if not role:
                            continue
                        mark = _Mark(child.tag, role, child.get("number") or "1", child,
                                     element, part_index, staff, measure_index, frac)
                        marks[part_index].append(mark)
                        if child.tag == "octave-shift":
                            if role == "start":
                                open_octaves[mark.number] = mark
                            else:
                                start = open_octaves.pop(mark.number, None)
                                if start is not None:
                                    mark.covered = start.covered
    return marks


def _pairs(marks: list[_Mark], report: TransplantReport) -> list[tuple[_Mark, ...]]:
    """Одиночные ремарки и пары «начало-конец» в порядке появления."""
    groups: list[tuple[_Mark, ...]] = []
    open_marks: dict[tuple[str, str], _Mark] = {}
    for mark in marks:
        if mark.role == "single":
            groups.append((mark,))
        elif mark.role == "start":
            key = (mark.kind, mark.number)
            if key in open_marks:
                report.skipped[f"{mark.kind}: начало без конца"] += 1
            open_marks[key] = mark
        else:
            start = open_marks.pop((mark.kind, mark.number), None)
            if start is None:
                report.skipped[f"{mark.kind}: конец без начала"] += 1
            else:
                groups.append((start, mark))
    for mark in open_marks.values():
        report.skipped[f"{mark.kind}: начало без конца"] += 1
    return groups


# ----------------------------------------------------------------------------------
# Куда ставить у homr
# ----------------------------------------------------------------------------------

@dataclass
class _Place:
    part: int
    staff: int
    measure: int
    onset: float          # в divisions такта homr
    frac: float


class _Mapper:
    def __init__(self, homr_path: Path, audiveris_path: Path, homr_root: ET.Element):
        comparison = compare(homr_path, audiveris_path)
        self.staffs = {
            row.reference: row.candidate for row in comparison.staves_rows
            if row.candidate is not None and row.accuracy >= _MIN_STAFF_ACCURACY
        }
        votes: dict[int, Counter] = defaultdict(Counter)
        for (part, _), (homr_part, _) in self.staffs.items():
            votes[part][homr_part] += 1
        self.parts = {part: counter.most_common(1)[0][0] for part, counter in votes.items()}
        theirs, ours = read_symbols(audiveris_path), read_symbols(homr_path)
        self.measures = {
            part: align_measures(theirs.bags.get(part, []), ours.bags.get(homr_part, []))
            for part, homr_part in self.parts.items()
        }
        self.homr_parts = homr_root.findall("part")

    def target_staff(self, part: int, staff: int) -> tuple[int, int] | None:
        if (part, staff) in self.staffs:
            return self.staffs[(part, staff)]
        if part in self.parts:
            return self.parts[part], 1
        return None

    def place(self, mark: _Mark, staff_override: tuple[int, int] | None = None) -> _Place | str:
        target = staff_override or self.target_staff(mark.part, mark.staff)
        if target is None:
            return "стан не сопоставлен"
        measure = self.measures.get(mark.part, {}).get(mark.measure)
        if measure is None:
            return "такт не сопоставлен"
        homr_part, homr_staff = target
        if homr_part >= len(self.homr_parts):
            return "стан не сопоставлен"
        measures = self.homr_parts[homr_part].findall("measure")
        if measure >= len(measures):
            return "такт не сопоставлен"
        items, longest, _ = _walk(measures[measure])
        onset = mark.frac * longest
        candidates = [item.onset for item in items
                      if item.element.tag == "note" and not item.grace
                      and item.staff == homr_staff]
        if candidates and longest > 0:
            nearest = min(candidates, key=lambda value: abs(value - onset))
            if abs(nearest - onset) <= _SNAP * longest:
                onset = nearest
        return _Place(homr_part, homr_staff, measure, onset,
                      onset / longest if longest > 0 else 0.0)

    def absolute(self, part: int, measure: int, frac: float) -> float | None:
        mapped = self.measures.get(part, {}).get(measure)
        return None if mapped is None else mapped + frac


def _new_direction(mark: _Mark, staff: int) -> ET.Element:
    direction = ET.Element("direction")
    placement = mark.direction.get("placement")
    if placement is None and mark.kind == "octave-shift":
        placement = "above" if mark.element.get("type") == "down" else "below"
    if placement is None and mark.kind in ("dynamics", "wedge", "pedal"):
        placement = "below"
    if placement:
        direction.set("placement", placement)
    direction_type = ET.SubElement(direction, "direction-type")
    child = copy.deepcopy(mark.element)
    for attribute in _LAYOUT_ATTRIBUTES:
        child.attrib.pop(attribute, None)
    child.tail = None
    direction_type.append(child)
    ET.SubElement(direction, "staff").text = str(staff)
    sound = mark.direction.find("sound")
    if sound is not None and mark.kind in ("dynamics", "pedal"):
        copied = copy.deepcopy(sound)
        copied.tail = None
        direction.append(copied)
    return direction


def _already_there(measure: ET.Element, place: _Place, mark: _Mark) -> bool:
    """Такая же ремарка уже стоит рядом на этом стане (перенос из второго файла).

    Audiveris может отдать страницу несколькими частями, а перенос бывает
    повторным — двойная динамика или вторая вилка поверх первой хуже, чем ничего.
    """
    items, longest, _ = _walk(measure)
    if longest <= 0:
        return False
    for item in items:
        element = item.element
        if element.tag != "direction" or item.staff not in (0, place.staff):
            continue
        if abs(item.onset - place.onset) > _SNAP * longest:
            continue
        for child in element.iter(mark.kind):
            if mark.kind == "dynamics":
                return True
            if (child.get("type") or "") == (mark.element.get("type") or ""):
                return True
    return False


def _insert(measure: ET.Element, place: _Place, direction: ET.Element) -> None:
    """Вставить `<direction>` в такт так, чтобы он пришёлся на `place.onset`."""
    items, longest, end_position = _walk(measure)
    children = list(measure)
    # Перед первой нотой нужного стана с этим моментом — позиция там ровно onset.
    for index, item in enumerate(items):
        if (item.element.tag == "note" and not item.chord and not item.grace
                and item.staff == place.staff and abs(item.onset - place.onset) < 1e-6):
            measure.insert(children.index(item.element), direction)
            return
    # Ноты на этом месте нет: в конец такта (перед правой чертой), со смещением.
    offset = place.onset - end_position
    if abs(offset) > 1e-6:
        element = ET.Element("offset")
        element.text = str(int(round(offset)))
        staff = direction.find("staff")
        direction.insert(list(direction).index(staff), element)
    barline = next((child for child in children
                    if child.tag == "barline" and child.get("location", "right") == "right"), None)
    if barline is not None:
        measure.insert(children.index(barline), direction)
    else:
        measure.append(direction)


def _shift_octave(homr_root: ET.Element, mapper: _Mapper, start: _Mark, place: _Place,
                  stop_place: _Place) -> int:
    """Сдвинуть на октаву ноты homr под линией. Возвращает, сколько сдвинуто."""
    size = int(start.element.get("size") or 8)
    octaves = {8: 1, 15: 2, 22: 3}.get(size, 1)
    delta = octaves if start.element.get("type") == "down" else -octaves
    spots = [mapper.absolute(start.part, measure, frac) for measure, frac in start.covered]
    spots = [spot for spot in spots if spot is not None]
    if not spots:
        return 0
    part = homr_root.findall("part")[place.part]
    shifted = 0
    first, last = place.measure, stop_place.measure
    for measure_index, measure in enumerate(part.findall("measure")):
        if measure_index < first or measure_index > last:
            continue
        items, longest, _ = _walk(measure)
        for item in items:
            element = item.element
            if element.tag != "note" or item.staff != place.staff:
                continue
            pitch = element.find("pitch")
            if pitch is None or longest <= 0:
                continue
            spot = measure_index + item.onset / longest
            if not any(abs(spot - other) <= _OCTAVE_EPS for other in spots):
                continue
            octave = pitch.find("octave")
            if octave is None or not (octave.text or "").strip().lstrip("-").isdigit():
                continue
            octave.text = str(int(octave.text) + delta)
            shifted += 1
    return shifted


def transplant(homr_path: Path, audiveris_path: Path, kinds=KINDS,
               output: Path | None = None) -> TransplantReport:
    """Дописать в `homr_path` ремарки из `audiveris_path` (или в `output`)."""
    report = TransplantReport()
    homr_path, audiveris_path = Path(homr_path), Path(audiveris_path)
    tree = ET.parse(str(homr_path))
    homr_root = tree.getroot()
    mapper = _Mapper(homr_path, audiveris_path, homr_root)
    marks = _collect_marks(load_root(audiveris_path), set(kinds))

    homr_parts = homr_root.findall("part")
    repeat_votes: dict[tuple[str, int], set[int]] = defaultdict(set)
    for part, part_marks in marks.items():
        for group in _pairs(part_marks, report):
            first = group[0]
            if first.kind == "repeat":
                measure = mapper.measures.get(first.part, {}).get(first.measure)
                if measure is None or first.part not in mapper.parts:
                    report.skipped["repeat: такт не сопоставлен"] += 1
                else:
                    repeat_votes[(first.number, measure)].add(mapper.parts[first.part])
                continue
            place = mapper.place(first)
            if isinstance(place, str):
                report.skipped[f"{first.kind}: {place}"] += len(group)
                continue
            places = [place]
            if len(group) == 2:
                stop = mapper.place(group[1], (place.part, place.staff))
                if isinstance(stop, str):
                    report.skipped[f"{first.kind}: {stop}"] += 2
                    continue
                if (stop.measure, stop.frac) < (place.measure, place.frac):
                    report.skipped[f"{first.kind}: конец раньше начала"] += 2
                    continue
                places.append(stop)
            measures = homr_parts[place.part].findall("measure")
            if _already_there(measures[place.measure], place, first):
                report.skipped[f"{first.kind}: уже есть"] += len(group)
                continue
            for mark, spot in zip(group, places):
                _insert(measures[spot.measure], spot, _new_direction(mark, spot.staff))
            report.added[first.kind] += 1
            if first.kind == "octave-shift" and len(group) == 2:
                report.shifted_notes += _shift_octave(homr_root, mapper, first, place, places[1])

    _apply_repeats(homr_parts, repeat_votes, len(set(mapper.parts.values())), report)

    if report.changed:
        tree.write(str(output or homr_path), encoding="utf-8", xml_declaration=True)
    return report


def _apply_repeats(homr_parts: list[ET.Element], votes: dict[tuple[str, int], set[int]],
                   voters: int, report: TransplantReport) -> None:
    """Повторы — решением на всю партитуру.

    Повтор меняет порядок исполнения, и стоять он должен во ВСЕХ партиях: повтор
    в части партий разводит их при развёртке. Поэтому ставим его во все партии,
    где его нет, если за него «проголосовало» большинство сопоставленных партий
    Audiveris; иначе не ставим нигде.
    """
    for (way, measure), parts in sorted(votes.items(), key=lambda item: (item[0][1], item[0][0])):
        if len(parts) * 2 < max(voters, 1):
            report.skipped["repeat: мало партий"] += 1
            continue
        added = False
        for part in homr_parts:
            measures = part.findall("measure")
            if measure >= len(measures) or _has_repeat(measures, measure, way):
                continue
            _add_repeat(measures[measure], way)
            added = True
        if added:
            report.added["repeat"] += 1
        else:
            report.skipped["repeat: уже есть у homr"] += 1


def _has_repeat(measures: list[ET.Element], index: int, way: str) -> bool:
    for neighbour in measures[max(index - 1, 0): index + 2]:
        for repeat in neighbour.iter("repeat"):
            if repeat.get("direction") == way:
                return True
    return False


def _add_repeat(measure: ET.Element, way: str) -> None:
    location = "left" if way == "forward" else "right"
    barline = next((b for b in measure.findall("barline")
                    if b.get("location", "right") == location), None)
    if barline is None:
        barline = ET.Element("barline", {"location": location})
        if location == "left":
            children = list(measure)
            index = 0
            while index < len(children) and children[index].tag in ("print", "attributes"):
                index += 1
            measure.insert(index, barline)
        else:
            measure.append(barline)
    ET.SubElement(barline, "repeat", {"direction": way})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omr.transplant")
    parser.add_argument("homr")
    parser.add_argument("audiveris")
    parser.add_argument("-o", "--output")
    args = parser.parse_args(argv)
    output = Path(args.output) if args.output else None
    if output is not None and not Path(args.homr).samefile(output):
        output.write_bytes(Path(args.homr).read_bytes())
    report = transplant(Path(output or args.homr), Path(args.audiveris))
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
