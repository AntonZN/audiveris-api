"""Приведение в порядок нотации в выходе homr: лиги, связки, повторы.

    python -m omr.notation score.musicxml [-o out.musicxml]

Лиги homr предсказывает (раннер возвращает их в MusicXML, см.
`_install_slurs_and_ties`), но сырые предсказания неряшливы, и отдавать их как
есть нельзя: несбалансированные лиги роняют verovio в мобильном клиенте.
Замер сырых лиг на `tests/images/symbols`: полнота 86%, точность 61%.

Что здесь делается:

1. **Лига между соседними нотами одной высоты — это связка.** Модель связок не
   выдаёт вовсе (ни одного токена `tieStart` на всём наборе), а связку рисует
   лигой: в Dichterliebe 37 пар лиг из 65 — соседние ноты одной высоты, при
   52 связках в эталоне. Без правки такая «лига» ещё и звучит двумя ударами.
   Связка получает и `<tied>` (рисовать), и `<tie>` (звучать).
2. **Пары лиг — по времени, по стану.** Конец без начала и начало без конца
   удаляются, перекрывающимся лигам раздаются номера.
3. **Forward-повтор на левую черту.** Генератор homr 0.6.2 вешает `repeatStart`
   на ПРАВУЮ черту нового такта — повтор съезжает на такт позже.
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class NotationReport:
    counts: Counter = field(default_factory=Counter)

    @property
    def changed(self) -> bool:
        return any(key != "лиг оставлено" for key in self.counts)

    def summary(self) -> str:
        if not self.counts:
            return "нотация: править нечего"
        return "нотация: " + ", ".join(f"{name} {count}" for name, count in self.counts.items())


@dataclass
class _Chord:
    measure: int
    onset: float
    order: int
    grace: bool
    notes: list[ET.Element] = field(default_factory=list)

    def pitches(self) -> dict[tuple, ET.Element]:
        result = {}
        for note in self.notes:
            pitch = note.find("pitch")
            if pitch is None:
                continue
            key = (pitch.findtext("step"), (pitch.findtext("octave") or "").strip(),
                   str(int(round(float(pitch.findtext("alter") or 0)))))
            result.setdefault(key, note)
        return result


def _chords_by_staff(part: ET.Element) -> dict[tuple[int, str], list[_Chord]]:
    """Аккорды по (стан, голос) в порядке звучания."""
    streams: dict[tuple[int, str], list[_Chord]] = {}
    order = 0
    for measure_index, measure in enumerate(part.findall("measure")):
        position = last_onset = 0.0
        last_chord: _Chord | None = None
        for element in measure:
            if element.tag == "backup":
                position -= float(element.findtext("duration") or 0)
            elif element.tag == "forward":
                position += float(element.findtext("duration") or 0)
            elif element.tag == "note":
                order += 1
                grace = element.find("grace") is not None
                duration = 0.0 if grace else float(element.findtext("duration") or 0)
                if element.find("chord") is not None and last_chord is not None:
                    last_chord.notes.append(element)
                    continue
                onset = last_onset = position
                position += duration
                staff = int((element.findtext("staff") or "1").strip() or 1)
                key = (staff, "")
                last_chord = _Chord(measure_index, onset, order, grace, [element])
                streams.setdefault(key, []).append(last_chord)
    for chords in streams.values():
        chords.sort(key=lambda chord: (chord.measure, chord.onset, not chord.grace, chord.order))
    return streams


def _notations(note: ET.Element) -> ET.Element:
    notations = note.find("notations")
    if notations is None:
        notations = ET.SubElement(note, "notations")
    return notations


def _drop(note: ET.Element, element: ET.Element) -> None:
    for notations in note.findall("notations"):
        if element in list(notations):
            notations.remove(element)
            if len(notations) == 0 and not (notations.text or "").strip():
                note.remove(notations)
            return


def _add_tie(note: ET.Element, kind: str) -> None:
    """`<tie>` (звук) после `<duration>` и `<tied>` (рисунок) в `<notations>`."""
    if not any(tie.get("type") == kind for tie in note.findall("tie")):
        children = list(note)
        anchor = note.find("duration")
        if anchor is None:
            anchor = note.find("pitch")
        index = children.index(anchor) + 1 if anchor is not None else len(children)
        tie = ET.Element("tie", {"type": kind})
        note.insert(index, tie)
    notations = _notations(note)
    if not any(tied.get("type") == kind for tied in notations.findall("tied")):
        ET.SubElement(notations, "tied", {"type": kind})


def _fix_slurs(part: ET.Element, report: NotationReport, *, ties_from_slurs: bool,
               pairing: str, max_measures: int | None, tie_rule: str) -> None:
    for chords in _chords_by_staff(part).values():
        open_slurs: list[tuple[int, ET.Element, ET.Element]] = []   # (индекс аккорда, нота, slur)
        pairs: list[tuple[int, ET.Element, ET.Element, int, ET.Element, ET.Element]] = []
        for index, chord in enumerate(chords):
            marks = [(note, slur) for note in chord.notes
                     for notations in note.findall("notations") for slur in notations.findall("slur")]
            for note, slur in sorted(marks, key=lambda item: item[1].get("type") != "stop"):
                if slur.get("type") == "stop":
                    if not open_slurs:
                        _drop(note, slur)
                        report.counts["лиг без начала удалено"] += 1
                        continue
                    start = open_slurs.pop(0 if pairing == "fifo" else -1)
                    pairs.append((*start, index, note, slur))
                elif slur.get("type") == "start":
                    open_slurs.append((index, note, slur))
                else:
                    _drop(note, slur)
        for _, note, slur in open_slurs:
            _drop(note, slur)
            report.counts["лиг без конца удалено"] += 1

        kept: list[tuple[int, int, ET.Element, ET.Element]] = []
        for start_index, start_note, start_slur, stop_index, stop_note, stop_slur in pairs:
            first, second = chords[start_index], chords[stop_index]
            if ties_from_slurs and _is_tie(chords, start_index, stop_index, tie_rule):
                common = set(first.pitches()) & set(second.pitches())
                _drop(start_note, start_slur)
                _drop(stop_note, stop_slur)
                for key in common:
                    _add_tie(first.pitches()[key], "start")
                    _add_tie(second.pitches()[key], "stop")
                report.counts["лиг стало связками"] += 1
                continue
            if max_measures is not None and second.measure - first.measure > max_measures:
                _drop(start_note, start_slur)
                _drop(stop_note, stop_slur)
                report.counts["слишком длинных лиг удалено"] += 1
                continue
            kept.append((start_index, stop_index, start_slur, stop_slur))

        # Номера перекрывающимся лигам: наименьший свободный на момент начала.
        active: list[tuple[int, int]] = []
        for start_index, stop_index, start_slur, stop_slur in sorted(kept, key=lambda item: item[:2]):
            active = [(end, number) for end, number in active if end > start_index]
            used = {number for _, number in active}
            number = next(value for value in range(1, 64) if value not in used)
            active.append((stop_index, number))
            start_slur.set("number", str(number))
            stop_slur.set("number", str(number))
        if kept:
            report.counts["лиг оставлено"] += len(kept)


def _is_tie(chords: list[_Chord], start_index: int, stop_index: int, rule: str = "equal") -> bool:
    """Лига на соседний аккорд той же высоты — это связка.

    `equal` — высоты аккордов совпадают целиком (одиночная нота — частный случай);
    `common` — хватает одного общего тона. Второе на Debussy превращало в связки
    настоящие фразовые лиги между аккордами с общим тоном.
    """
    first, second = chords[start_index], chords[stop_index]
    if first.grace or second.grace:
        return False
    following = next((i for i in range(start_index + 1, len(chords)) if not chords[i].grace), None)
    if following != stop_index:
        return False
    ours, theirs = set(first.pitches()), set(second.pitches())
    if rule == "common":
        return bool(ours & theirs)
    return bool(ours) and ours == theirs


def _fix_forward_repeats(part: ET.Element, report: NotationReport) -> None:
    for measure in part.findall("measure"):
        for barline in measure.findall("barline"):
            repeat = barline.find("repeat")
            if (barline.get("location", "right") != "right" or repeat is None
                    or repeat.get("direction") != "forward"):
                continue
            barline.remove(repeat)
            if len(barline) == 0:
                measure.remove(barline)
            left = next((b for b in measure.findall("barline") if b.get("location") == "left"), None)
            if left is None:
                left = ET.Element("barline", {"location": "left"})
                children = list(measure)
                index = 0
                while index < len(children) and children[index].tag in ("print", "attributes"):
                    index += 1
                measure.insert(index, left)
            if left.find("repeat") is None:
                ET.SubElement(left, "repeat", {"direction": "forward"})
            report.counts["forward-повтор перенесён на левую черту"] += 1


def normalize(path: Path, output: Path | None = None, *, ties_from_slurs: bool = True,
              pairing: str = "lifo", max_measures: int | None = None,
              tie_rule: str = "equal") -> NotationReport:
    report = NotationReport()
    tree = ET.parse(str(path))
    for part in tree.getroot().findall("part"):
        _fix_slurs(part, report, ties_from_slurs=ties_from_slurs, pairing=pairing,
                   max_measures=max_measures, tie_rule=tie_rule)
        _fix_forward_repeats(part, report)
    if report.changed or output is not None:
        tree.write(str(output or path), encoding="utf-8", xml_declaration=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omr.notation")
    parser.add_argument("musicxml")
    parser.add_argument("-o", "--output")
    parser.add_argument("--no-ties", action="store_true")
    parser.add_argument("--pairing", choices=("fifo", "lifo"), default="lifo")
    parser.add_argument("--tie-rule", choices=("equal", "common"), default="equal")
    parser.add_argument("--max-measures", type=int)
    args = parser.parse_args(argv)
    report = normalize(Path(args.musicxml), Path(args.output) if args.output else None,
                       ties_from_slurs=not args.no_ties, pairing=args.pairing,
                       max_measures=args.max_measures, tie_rule=args.tie_rule)
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
