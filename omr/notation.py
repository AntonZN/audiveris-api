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
4. **Наезды внутри голоса.** Одновременные события на двух станах генератор
   homr выписывает и сдвигает курсор на САМУЮ КОРОТКУЮ длительность, а более
   длинное событие продолжает звучать — и следующая нота того же стана и голоса
   начинается раньше, чем оно кончилось. MuseScore такой файл не открывает
   («файл повреждён», код 40 в CLI): MozaVeil с прода — такты 3 и 17. Время в
   MusicXML задаёт курсор (`backup`/`forward`), а голос — только метка, поэтому
   длинное событие переводится в свободный голос того же стана, ни одна нота во
   времени не сдвигается. Пауза, уехавшая в отдельный голос, делается невидимой.
5. **Тремоло.** Токен модели `tremolo` относится к ОДНОЙ ноте, а генератор homr
   чередует `type="start"`/`"stop"` по всему файлу — одиночное тремоло
   становится «двухнотным» между чужими нотами, в том числе через тактовую
   черту. MuseScore такой файл не открывает (Гайдн, Брамс соч. 99). Пишем
   `single`.
6. **Партии разной длины** (jungle: 22, 20 и 20 тактов на одной странице)
   дополняются тактами-паузами до самой длинной: партитура с разным числом
   тактов в партиях — тоже отказ MuseScore.
7. **Скобки триолей.** homr пишет `<tuplet type="start">` и не закрывает группу,
   а неполную группу триолей (модель ошиблась в длительности) MuseScore без явной
   скобки не открывает: две триольные 16-е подряд — отказ, те же ноты между
   `start` и `stop` — принимаются (синтетика, CLI MuseScore 4.6). Скобки
   пересобираются по голосам: подряд идущие ноты с одним `time-modification` —
   группами полного размера, остаток последней группой. Длительности не
   меняются. Брамса соч. 99 (2-я часть, т.8) это не спасает: там триоль
   начинается за концом переполненного такта, и что именно MuseScore в нём не
   нравится, по синтетике установить не удалось.
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


@dataclass
class _Event:
    onset: float
    end: float
    staff: str
    voice: str
    order: int
    rest: bool
    notes: list[ET.Element] = field(default_factory=list)


def _voice_events(measure: ET.Element) -> list[_Event]:
    """Голова аккорда (с членами) — одно событие; форшлаги времени не занимают."""
    events: list[_Event] = []
    position = 0.0
    last: _Event | None = None
    for order, element in enumerate(measure):
        if element.tag == "backup":
            position -= float(element.findtext("duration") or 0)
        elif element.tag == "forward":
            position += float(element.findtext("duration") or 0)
        elif element.tag == "note":
            if element.find("grace") is not None:
                continue
            if element.find("chord") is not None and last is not None:
                last.notes.append(element)
                continue
            duration = float(element.findtext("duration") or 0)
            last = _Event(position, position + duration,
                          (element.findtext("staff") or "1").strip(),
                          (element.findtext("voice") or "1").strip(),
                          order, element.find("rest") is not None, [element])
            events.append(last)
            position += duration
    return events


def _set_voice(note: ET.Element, voice: str) -> None:
    element = note.find("voice")
    if element is None:
        element = ET.Element("voice")
        anchor = note.find("duration")
        children = list(note)
        note.insert(children.index(anchor) + 1 if anchor is not None else len(children), element)
    element.text = voice


# MuseScore держит не больше четырёх голосов на стан, а номера голосов раскладывает
# по станам на всю партию: homr 0.6.2 пишет на нижнем стане и `1`, и `5`, и
# разведение наездов новыми номерами (у Dichterliebe дошло до 16) ломало файл
# накопительно — каждый такт по отдельности принимался, первые пять вместе нет.
_VOICES_PER_STAFF = 4


def _fix_voice_overlaps(part: ET.Element, report: NotationReport) -> None:
    """Разложить события каждого стана по четырём дорожкам без наездов.

    Номер голоса — (стан − 1) · 4 + дорожка: у каждого стана свои 1–4 / 5–8, как
    в новых версиях homr. Событие идёт на «свою» дорожку (по порядку появления
    его голоса в такте). Если там мешает пауза или более длинная выдержанная нота,
    с дороги уходит она — так мелодия остаётся в своём голосе; иначе на
    свободную дорожку идёт само событие. Время не трогается: курсор задают
    `backup`/`forward`, голос — только метка. Пауза, ушедшая со своей дорожки,
    становится невидимой.
    """
    eps = 1e-9

    def free(lane: list[_Event], event: _Event) -> bool:
        return all(other.end <= event.onset + eps or other.onset >= event.end - eps
                   for other in lane)

    for measure in part.findall("measure"):
        events = _voice_events(measure)
        graces = _grace_owners(measure)
        by_staff: dict[str, list[_Event]] = {}
        for event in events:
            by_staff.setdefault(event.staff, []).append(event)
        for staff, staff_events in by_staff.items():
            base = (int(staff) - 1) * _VOICES_PER_STAFF if staff.isdigit() else 0
            preferred: dict[str, int] = {}
            for event in sorted(staff_events, key=lambda e: e.order):
                if event.voice not in preferred and len(preferred) < _VOICES_PER_STAFF:
                    preferred[event.voice] = len(preferred) + 1
            lanes: dict[int, list[_Event]] = {slot: [] for slot in range(1, _VOICES_PER_STAFF + 1)}
            slot_of: dict[int, int] = {}
            for event in sorted(staff_events, key=lambda e: (e.onset, e.order)):
                wish = preferred.get(event.voice, 1)
                slot = wish if free(lanes[wish], event) else None
                if slot is None:
                    blockers = [other for other in lanes[wish] if not free([other], event)]
                    if len(blockers) == 1 and (blockers[0].rest or blockers[0].end > event.end + eps):
                        blocker = blockers[0]
                        refuge = next((s for s in lanes if s != wish
                                       and free(lanes[s], blocker)), None)
                        if refuge is not None:
                            lanes[wish].remove(blocker)
                            lanes[refuge].append(blocker)
                            slot_of[id(blocker)] = refuge
                            slot = wish
                            report.counts["наездов в голосе разведено"] += 1
                if slot is None:
                    slot = next((s for s in lanes if s != wish and free(lanes[s], event)), None)
                    if slot is None:
                        slot = wish
                        report.counts["наездов не разведено (больше 4 голосов)"] += 1
                    else:
                        report.counts["наездов в голосе разведено"] += 1
                lanes[slot].append(event)
                slot_of[id(event)] = slot
            for event in staff_events:
                slot = slot_of[id(event)]
                wish = preferred.get(event.voice, 1)
                if event.rest and slot != wish:
                    for note in event.notes:
                        note.set("print-object", "no")
                label = str(base + slot)
                for note in event.notes + graces.get(event.notes[0], []):
                    if (note.findtext("voice") or "").strip() != label:
                        _set_voice(note, label)
                        report.counts["голосов перенумеровано"] += 1


def _grace_owners(measure: ET.Element) -> dict[ET.Element, list[ET.Element]]:
    """Форшлаги — к следующей основной ноте того же стана: голос у них общий."""
    owners: dict[ET.Element, list[ET.Element]] = {}
    waiting: dict[str, list[ET.Element]] = {}
    for element in measure:
        if element.tag != "note":
            continue
        staff = (element.findtext("staff") or "1").strip()
        if element.find("grace") is not None:
            waiting.setdefault(staff, []).append(element)
        elif element.find("chord") is None and waiting.get(staff):
            owners[element] = waiting.pop(staff)
    return owners


def _fix_tremolos(part: ET.Element, report: NotationReport) -> None:
    for tremolo in part.iter("tremolo"):
        if tremolo.get("type") in ("start", "stop"):
            tremolo.set("type", "single")
            report.counts["тремоло стало одиночным"] += 1


def _pad_parts(root: ET.Element, report: NotationReport) -> None:
    parts = root.findall("part")
    if len(parts) < 2:
        return
    longest = max(len(part.findall("measure")) for part in parts)
    for part in parts:
        measures = part.findall("measure")
        missing = longest - len(measures)
        if missing <= 0 or not measures:
            continue
        divisions, beats, beat_type = 1, 4, 4
        for attributes in part.iter("attributes"):
            for tag, current in (("divisions", divisions), ("time/beats", beats),
                                 ("time/beat-type", beat_type)):
                text = (attributes.findtext(tag) or "").strip()
                if text.isdigit() and int(text) > 0:
                    if tag == "divisions":
                        divisions = int(text)
                    elif tag == "time/beats":
                        beats = int(text)
                    else:
                        beat_type = int(text)
        length = max(1, divisions * beats * 4 // beat_type)
        number = len(measures)
        for _ in range(missing):
            number += 1
            measure = ET.SubElement(part, "measure", {"number": str(number)})
            note = ET.SubElement(measure, "note")
            ET.SubElement(note, "rest", {"measure": "yes"})
            ET.SubElement(note, "duration").text = str(length)
            ET.SubElement(note, "voice").text = "1"
        report.counts["тактов-пауз добавлено в короткие партии"] += missing


def _fix_tuplet_brackets(part: ET.Element, report: NotationReport) -> None:
    for measure in part.findall("measure"):
        lanes: dict[tuple[str, str], list[_Event]] = {}
        for event in _voice_events(measure):
            lanes.setdefault((event.staff, event.voice), []).append(event)
        for events in lanes.values():
            groups: list[list[_Event]] = []
            run: list[_Event] = []
            ratio = None
            for event in sorted(events, key=lambda e: (e.onset, e.order)):
                modification = event.notes[0].find("time-modification")
                current = None if modification is None else (
                    modification.findtext("actual-notes"), modification.findtext("normal-notes"))
                if current is not None and run and current == ratio \
                        and abs(run[-1].end - event.onset) < 1e-9:
                    run.append(event)
                    continue
                groups += _split_tuplet_run(run, ratio)
                run, ratio = ([event], current) if current is not None else ([], None)
            groups += _split_tuplet_run(run, ratio)
            for group in groups:
                if _set_tuplet_bracket(group):
                    report.counts["скобок триолей пересобрано"] += 1


def _split_tuplet_run(run: list[_Event], ratio) -> list[list[_Event]]:
    """Ряд триольных нот -> группы полного размера (actual × самая короткая), остаток — последней."""
    if not run or ratio is None or not (ratio[0] or "").isdigit():
        return []
    unit = min(event.end - event.onset for event in run)
    size = unit * int(ratio[0])
    groups, current, filled = [], [], 0.0
    for event in run:
        current.append(event)
        filled += event.end - event.onset
        if size > 0 and filled >= size - 1e-9:
            groups.append(current)
            current, filled = [], 0.0
    if current:
        groups.append(current)
    return groups


def _set_tuplet_bracket(group: list[_Event]) -> bool:
    """Скобка start на первой ноте группы и stop на последней; остальные <tuplet> — долой."""
    wanted = {id(group[0].notes[0]): "start", id(group[-1].notes[0]): "stop"}
    if len(group) == 1:
        wanted = {id(group[0].notes[0]): "start"}
    changed = False
    for event in group:
        for note in event.notes:
            for notations in note.findall("notations"):
                for tuplet in notations.findall("tuplet"):
                    if wanted.get(id(note)) != tuplet.get("type"):
                        notations.remove(tuplet)
                        changed = True
    for note_id, kind in wanted.items():
        note = next(n for event in group for n in event.notes if id(n) == note_id)
        notations = _notations(note)
        if not any(t.get("type") == kind for t in notations.findall("tuplet")):
            ET.SubElement(notations, "tuplet", {"type": kind})
            changed = True
    if len(group) == 1:
        # Одинокая триольная нота: и начало, и конец на ней же.
        notations = _notations(group[0].notes[0])
        if not any(t.get("type") == "stop" for t in notations.findall("tuplet")):
            ET.SubElement(notations, "tuplet", {"type": "stop"})
            changed = True
    return changed


def normalize(path: Path, output: Path | None = None, *, ties_from_slurs: bool = True,
              pairing: str = "lifo", max_measures: int | None = None,
              tie_rule: str = "equal") -> NotationReport:
    report = NotationReport()
    tree = ET.parse(str(path))
    _pad_parts(tree.getroot(), report)
    for part in tree.getroot().findall("part"):
        _fix_tremolos(part, report)
        _fix_voice_overlaps(part, report)
        _fix_tuplet_brackets(part, report)
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
