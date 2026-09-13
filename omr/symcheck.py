"""Сверка СИМВОЛОВ с эталонным MusicXML: динамика, вилки, лиги, штрихи, 8va…

    python -m omr.symcheck out.musicxml ref.musicxml          # одна пара
    python -m omr.symcheck --runs out/ tests/images/symbols    # выходы refcheck

`omr/refcheck.py` меряет НОТЫ: высоты, станы, одновременность рук. Жалобы же
пришли на всё, что вокруг нот: pp/f и sf, вилки cresc/dim, длинные лиги,
акценты, 8va, повторы, форшлаги, триоли, смены ключа, альтерация, cross-staff.
Одна цифра по нотам этого не видит: партитура без единой динамики даёт 100%.

Как считается. Каждый символ — событие «вид, партия, стан, такт + доля такта».
Такты эталона и выхода выравниваются по содержимому (мешок высот такта,
динамическое программирование со вставками/пропусками): выход часто теряет или
добавляет такт, и сравнение «такт N с тактом N» после первой же ошибки
разваливается. Дальше событие эталона ищет событие того же вида в
соответствующем такте выхода с допуском по доле:

* привязанное к ноте (лига, связка, штрих, триоль, форшлаг) — тот же стан и
  почти то же место;
* ремарка (динамика, вилка, 8va, педаль) — та же партия, любой стан (у
  фортепиано динамика стоит между станами, и стан в разметке условен), допуск
  шире — ремарку ставят и на долю раньше ноты;
* повтор, вольта, смена ключа — место в такте.

Отдельные виды меряют не символ, а его СЛЕДСТВИЕ для звука, потому что сам знак
плееру не нужен:

* `alter` — нота эталона со знаком альтерации: есть ли в выходе нота той же
  высоты (с учётом знака) там же. `<accidental>` сам по себе движки не пишут,
  verovio выводит его из высоты;
* `8va-pitch` — нота под октавной линией: высота в MusicXML звучащая, и если
  линия не прочитана, нота выйдет на октаву не там;
* `cross-staff` — нота голоса, заходящая на чужой стан: оказалась ли она в
  выходе на правильном стане.

Точность для этих трёх не считается — там нечего считать «лишним».
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from omr.refcheck import _DEGREE, _SEMITONE, compare, load_root, reference_for

# Виды событий в порядке отчёта; группа — как считать совпадение.
NOTE_KINDS = ("slur", "slur-long", "tie", "accent", "strong-accent", "staccato",
              "staccatissimo", "tenuto", "fermata", "arpeggiate", "trill", "tuplet",
              "grace", "cue")
DIRECTION_KINDS = ("dynamics", "sforzando", "cresc", "dim", "cresc-text", "dim-text",
                   "8va", "pedal")
BAR_KINDS = ("repeat-fwd", "repeat-bwd", "ending", "clef-change")
PITCH_KINDS = ("alter", "8va-pitch", "cross-staff")
KINDS = DIRECTION_KINDS + NOTE_KINDS + BAR_KINDS + PITCH_KINDS

# Допуск по доле такта. Нота 16-й в 4/4 — это 1/16 = 0.0625 такта.
_TOLERANCE = {"note": 0.07, "direction": 0.3, "bar": 0.3, "pitch": 0.07}

_SFORZANDO = {"sf", "sfz", "sffz", "fz", "rfz", "rf", "sfp", "sfzp"}
_ARTICULATIONS = {"accent", "strong-accent", "staccato", "staccatissimo", "tenuto"}


def _group(kind: str) -> str:
    if kind in NOTE_KINDS:
        return "note"
    if kind in DIRECTION_KINDS:
        return "direction"
    if kind in BAR_KINDS:
        return "bar"
    return "pitch"


@dataclass
class Event:
    kind: str
    part: int
    staff: int
    measure: int
    frac: float = 0.0
    detail: str = ""      # тип динамики, направление 8va…
    midi: int = -1        # для нот и видов «по высоте»


@dataclass
class Symbols:
    events: list[Event] = field(default_factory=list)
    notes: list[Event] = field(default_factory=list)          # все ноты с высотой
    bags: dict = field(default_factory=dict)                    # партия -> [Counter по тактам]


def _midi(pitch) -> int:
    step = pitch.findtext("step") or "C"
    octave = int(pitch.findtext("octave") or 4)
    alter = int(round(float(pitch.findtext("alter") or 0)))
    return 12 * (octave + 1) + _SEMITONE.get(step, 0) + alter


def read_symbols(path: Path) -> Symbols:
    root = load_root(path)
    result = Symbols()
    for part_index, part in enumerate(root.findall("part")):
        divisions = 1.0
        clefs: dict[int, str] = {}
        open_slurs: dict[str, Event] = {}
        open_octave: dict[str, tuple[int, float, int]] = {}   # номер -> (такт, доля, сдвиг)
        octave_spans: list[tuple[float, float, int]] = []
        bags: list[Counter] = []
        part_notes: list[Event] = []
        for measure_index, measure in enumerate(part.findall("measure")):
            position = longest = last_onset = 0.0
            pending: list[tuple[float, Event]] = []
            voices: dict[str, Counter] = defaultdict(Counter)
            voiced: list[tuple[str, Event]] = []
            bag: Counter = Counter()

            def add(onset: float, event: Event) -> Event:
                pending.append((onset, event))
                return event

            for element in measure:
                tag = element.tag
                if tag == "attributes":
                    try:
                        value = float((element.findtext("divisions") or "").strip())
                        if value > 0:
                            divisions = value
                    except ValueError:
                        pass
                    for clef in element.findall("clef"):
                        number = int(clef.get("number") or 1)
                        name = (clef.findtext("sign") or "?") + (clef.findtext("line") or "")
                        if number in clefs and clefs[number] != name:
                            add(position, Event("clef-change", part_index, number,
                                                measure_index, detail=name))
                        clefs[number] = name
                elif tag == "backup":
                    position -= float(element.findtext("duration") or 0)
                elif tag == "forward":
                    position += float(element.findtext("duration") or 0)
                    longest = max(longest, position)
                elif tag == "direction":
                    staff = int((element.findtext("staff") or "1").strip() or 1)
                    onset = position + float(element.findtext("offset") or 0)
                    for kind_element in element.iter():
                        name = kind_element.tag
                        if name == "dynamics":
                            for mark in kind_element:
                                detail = mark.tag if mark.tag != "other-dynamics" else (mark.text or "")
                                kind = "sforzando" if detail in _SFORZANDO else "dynamics"
                                add(onset, Event(kind, part_index, staff, measure_index,
                                                 detail=detail))
                        elif name == "wedge":
                            wedge = kind_element.get("type")
                            if wedge == "crescendo":
                                add(onset, Event("cresc", part_index, staff, measure_index))
                            elif wedge == "diminuendo":
                                add(onset, Event("dim", part_index, staff, measure_index))
                        elif name == "words":
                            text = (kind_element.text or "").strip().lower()
                            if re.match(r"(poco\s+)?cresc", text):
                                add(onset, Event("cresc-text", part_index, staff, measure_index))
                            elif re.match(r"(poco\s+)?(dim|decresc)", text):
                                add(onset, Event("dim-text", part_index, staff, measure_index))
                        elif name == "octave-shift":
                            shift_type = kind_element.get("type")
                            number = kind_element.get("number") or "1"
                            size = int(kind_element.get("size") or 8)
                            octaves = {8: 1, 15: 2, 22: 3}.get(size, 1)
                            if shift_type in ("up", "down"):
                                sign = 1 if shift_type == "down" else -1
                                add(onset, Event("8va", part_index, staff, measure_index,
                                                 detail=f"{shift_type}{size}"))
                                open_octave[number] = (measure_index, onset, sign * octaves)
                            elif shift_type == "stop" and number in open_octave:
                                start_measure, start_onset, octaves = open_octave.pop(number)
                                octave_spans.append((start_measure, start_onset,
                                                     measure_index, onset, octaves))
                        elif name == "pedal" and kind_element.get("type") in ("start", "sostenuto"):
                            add(onset, Event("pedal", part_index, staff, measure_index))
                elif tag == "barline":
                    at_end = element.get("location", "right") == "right"
                    repeat = element.find("repeat")
                    if repeat is not None:
                        kind = "repeat-fwd" if repeat.get("direction") == "forward" else "repeat-bwd"
                        add(-1.0 if not at_end else float("inf"),
                            Event(kind, part_index, 1, measure_index))
                    ending = element.find("ending")
                    if ending is not None and ending.get("type") == "start":
                        add(-1.0, Event("ending", part_index, 1, measure_index,
                                        detail=ending.get("number") or ""))
                elif tag == "note":
                    grace = element.find("grace") is not None
                    duration = 0.0 if grace else float(element.findtext("duration") or 0)
                    if element.find("chord") is not None:
                        onset = last_onset
                    else:
                        onset = last_onset = position
                        position += duration
                    longest = max(longest, position)
                    staff = int((element.findtext("staff") or "1").strip() or 1)
                    pitch = element.find("pitch")
                    midi = _midi(pitch) if pitch is not None else -1

                    def note_event(kind: str, detail: str = "") -> Event:
                        return add(onset, Event(kind, part_index, staff, measure_index,
                                                detail=detail, midi=midi))

                    if pitch is not None:
                        event = Event("note", part_index, staff, measure_index, midi=midi)
                        pending.append((onset, event))
                        part_notes.append(event)
                        voiced.append((element.findtext("voice") or "1", event))
                        voices[element.findtext("voice") or "1"][staff] += 1
                        if not grace:
                            bag[midi] += 1
                        if element.find("accidental") is not None:
                            note_event("alter")
                    if grace:
                        note_event("grace")
                    type_element = element.find("type")
                    if element.find("cue") is not None or (
                            type_element is not None and type_element.get("size") == "cue"):
                        note_event("cue")
                    if element.find("time-modification") is not None:
                        note_event("tuplet")
                    for notations in element.findall("notations"):
                        for mark in notations.iter():
                            name = mark.tag
                            if name == "slur":
                                number = mark.get("number") or "1"
                                if mark.get("type") == "start":
                                    open_slurs[number] = note_event("slur")
                                elif mark.get("type") == "stop" and number in open_slurs:
                                    start = open_slurs.pop(number)
                                    if measure_index > start.measure:
                                        note_event("slur-long-stop")
                                        start.detail = "long"
                            elif name == "tied" and mark.get("type") == "start":
                                note_event("tie")
                            elif name in _ARTICULATIONS or name in ("fermata", "arpeggiate"):
                                note_event(name)
                            elif name in ("trill-mark", "wavy-line") and (
                                    name == "trill-mark" or mark.get("type") == "start"):
                                note_event("trill")
            # Нота на чужом стане своего голоса — cross-staff.
            for voice, event in voiced:
                home = voices[voice].most_common(1)[0][0]
                if event.staff != home:
                    pending.append((0.0, Event("cross-staff", part_index, event.staff,
                                               measure_index, midi=event.midi)))
                    pending[-1] = (_onset_of(pending, event), pending[-1][1])
            for onset, event in pending:
                if onset == float("inf"):
                    event.frac = 1.0
                elif onset < 0:
                    event.frac = 0.0
                else:
                    event.frac = onset / longest if longest > 0 else 0.0
                if event.kind == "note":
                    continue
                if event.kind == "slur-long-stop":
                    continue
                result.events.append(event)
            bags.append(bag)

        # Длинная лига — отдельным видом рядом с обычной.
        for event in list(result.events):
            if event.part == part_index and event.kind == "slur" and event.detail == "long":
                result.events.append(Event("slur-long", part_index, event.staff,
                                           event.measure, event.frac, midi=event.midi))
        # Ноты под октавной линией.
        for note in part_notes:
            where = note.measure + note.frac
            for start_measure, start_onset, end_measure, end_onset, octaves in octave_spans:
                if start_measure <= note.measure <= end_measure:
                    result.events.append(Event("8va-pitch", part_index, note.staff,
                                               note.measure, note.frac, midi=note.midi))
                    break
            del where
        result.notes.extend(part_notes)
        result.bags[part_index] = bags
    return result


def _onset_of(pending: list[tuple[float, Event]], event: Event) -> float:
    for onset, candidate in pending:
        if candidate is event:
            return onset
    return 0.0


# ----------------------------------------------------------------------------------
# Выравнивание тактов
# ----------------------------------------------------------------------------------

def _bag_cost(a: Counter, b: Counter) -> float:
    if not a and not b:
        return 0.3
    common = sum((a & b).values())
    if not common:
        return 1.0
    precision, recall = common / max(sum(b.values()), 1), common / max(sum(a.values()), 1)
    return 1.0 - 2 * precision * recall / (precision + recall)


def align_measures(reference: list[Counter], candidate: list[Counter],
                   gap: float = 0.6) -> dict[int, int]:
    """Такт эталона -> такт выхода (пропущенные такты эталона в словарь не входят)."""
    n, m = len(reference), len(candidate)
    if not n or not m:
        return {}
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    step = [[0] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(1, n + 1):
        cost[i][0], step[i][0] = i * gap, 1
    for j in range(1, m + 1):
        cost[0][j], step[0][j] = j * gap, 2
    for i in range(1, n + 1):
        row, previous = cost[i], cost[i - 1]
        for j in range(1, m + 1):
            match = previous[j - 1] + _bag_cost(reference[i - 1], candidate[j - 1])
            skip_reference = previous[j] + gap
            skip_candidate = row[j - 1] + gap
            best = min(match, skip_reference, skip_candidate)
            row[j] = best
            step[i][j] = 0 if best == match else (1 if best == skip_reference else 2)
    mapping: dict[int, int] = {}
    i, j = n, m
    while i > 0 and j > 0:
        if step[i][j] == 0:
            if _bag_cost(reference[i - 1], candidate[j - 1]) < 0.95:
                mapping[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif step[i][j] == 1:
            i -= 1
        else:
            j -= 1
    return mapping


# ----------------------------------------------------------------------------------
# Сопоставление
# ----------------------------------------------------------------------------------

@dataclass
class KindScore:
    reference: int = 0
    candidate: int = 0
    matched: int = 0          # найдено из эталона
    true_candidate: int = 0   # подтверждено из выхода
    exact: int = 0            # для динамики: совпал и тип

    def add(self, other: "KindScore") -> None:
        for name in ("reference", "candidate", "matched", "true_candidate", "exact"):
            setattr(self, name, getattr(self, name) + getattr(other, name))

    @property
    def recall(self) -> float | None:
        return self.matched / self.reference if self.reference else None

    @property
    def precision(self) -> float | None:
        return self.true_candidate / self.candidate if self.candidate else None


@dataclass
class SymbolComparison:
    scores: dict[str, KindScore]
    part_map: dict[int, int]
    staff_map: dict[tuple[int, int], tuple[int, int]]

    def report(self, kinds=KINDS) -> str:
        lines = [f"{'символ':14s} {'эталон':>7s} {'выход':>6s} {'нашли':>6s} "
                 f"{'полнота':>8s} {'точность':>9s}"]
        for kind in kinds:
            score = self.scores.get(kind)
            if score is None or (not score.reference and not score.candidate):
                continue
            lines.append(_row(kind, score))
        return "\n".join(lines)


def _row(kind: str, score: KindScore) -> str:
    recall = "-" if score.recall is None else f"{score.recall * 100:.0f}%"
    precision = ("-" if score.precision is None or _group(kind) == "pitch"
                 else f"{score.precision * 100:.0f}%")
    extra = ""
    if kind in ("dynamics", "sforzando") and score.matched:
        extra = f"  (тип верен {score.exact}/{score.matched})"
    candidate = "-" if _group(kind) == "pitch" else str(score.candidate)
    return (f"{kind:14s} {score.reference:7d} {candidate:>6s} {score.matched:6d} "
            f"{recall:>8s} {precision:>9s}{extra}")


def compare_symbols(candidate_path: Path, reference_path: Path) -> SymbolComparison:
    notes_comparison = compare(candidate_path, reference_path)
    staff_map = {row.reference: row.candidate for row in notes_comparison.staves_rows
                 if row.candidate is not None}
    # Партия эталона -> партия выхода: куда ушло больше всего её станов (по нотам).
    votes: dict[int, Counter] = defaultdict(Counter)
    for row in notes_comparison.staves_rows:
        if row.candidate is not None:
            votes[row.reference[0]][row.candidate[0]] += row.reference_notes
    part_map = {part: counter.most_common(1)[0][0] for part, counter in votes.items()}

    reference = read_symbols(reference_path)
    candidate = read_symbols(candidate_path)

    alignments = {
        part: align_measures(reference.bags.get(part, []), candidate.bags.get(cpart, []))
        for part, cpart in part_map.items()
    }

    by_kind_candidate: dict[str, list[Event]] = defaultdict(list)
    for event in candidate.events:
        by_kind_candidate[event.kind].append(event)
    notes_by_part: dict[int, list[Event]] = defaultdict(list)
    for note in candidate.notes:
        notes_by_part[note.part].append(note)

    scores = {kind: KindScore() for kind in KINDS}
    # «Лишним» считается только то, что стоит там, где эталон вообще есть: в партии
    # выхода, сопоставленной партии эталона, и в такте, выровненном с тактом
    # эталона. Эталон бывает фрагментом (Шуберт), одной партией из ансамбля
    # (виолончель без фортепиано) или одной из двух редакций в PDF (Гайдн) — иначе
    # всё за его пределами записывалось бы в ложные срабатывания.
    covered = {(cpart, cmeasure) for part, cpart in part_map.items()
               for cmeasure in alignments.get(part, {}).values()}
    for event in candidate.events:
        if event.kind in scores and _group(event.kind) != "pitch" \
                and (event.part, event.measure) in covered:
            scores[event.kind].candidate += 1

    used: set[int] = set()
    for event in reference.events:
        if event.kind not in scores:
            continue
        score = scores[event.kind]
        score.reference += 1
        cpart = part_map.get(event.part)
        mapping = alignments.get(event.part, {})
        if cpart is None or event.measure not in mapping:
            continue
        where = mapping[event.measure] + event.frac
        group = _group(event.kind)
        tolerance = _TOLERANCE[group]
        cstaff = staff_map.get((event.part, event.staff))

        if group == "pitch":
            pool = notes_by_part.get(cpart, [])
            need_staff = event.kind == "cross-staff"
            hit = any(
                note.midi == event.midi
                and abs(note.measure + note.frac - where) <= tolerance
                and (not need_staff or (cstaff is not None and note.staff == cstaff[1]))
                for note in pool
            )
            score.matched += hit
            continue

        best, best_distance = None, None
        for other in by_kind_candidate.get(event.kind, []):
            if id(other) in used or other.part != cpart \
                    or (other.part, other.measure) not in covered:
                continue
            if group == "note" and cstaff is not None and other.staff != cstaff[1]:
                continue
            distance = abs(other.measure + other.frac - where)
            if distance <= tolerance and (best_distance is None or distance < best_distance):
                best, best_distance = other, distance
        if best is not None:
            used.add(id(best))
            score.matched += 1
            score.true_candidate += 1
            if best.detail == event.detail:
                score.exact += 1
    return SymbolComparison(scores, part_map, staff_map)


# ----------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------

def _runs(output_dir: Path, inputs_dir: Path) -> list[tuple[str, Path, Path]]:
    """Пары (имя, выход, эталон) для папки выходов `omr.refcheck` или Audiveris."""
    pairs = []
    for source in sorted(inputs_dir.iterdir()):
        if source.suffix.lower() not in (".pdf", ".png", ".jpg", ".jpeg"):
            continue
        reference = reference_for(source)
        if reference is None:
            reference = next((p for p in inputs_dir.iterdir()
                              if p.suffix in (".musicxml", ".xml", ".mxl")
                              and source.stem.split("-")[0] in p.stem), None)
        folder = output_dir / source.stem
        produced = [folder / f"{source.stem}.musicxml", *sorted(folder.glob("*.mxl"))]
        produced = [p for p in produced if p.exists()]
        if reference is None or not produced:
            continue
        pairs.append((source.stem, produced[0], reference))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omr.symcheck")
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--runs", nargs=2, metavar=("OUT_DIR", "INPUTS_DIR"),
                        help="сверить все выходы в OUT_DIR/<stem>/ с эталонами в INPUTS_DIR")
    parser.add_argument("--json", help="сохранить цифры в JSON")
    args = parser.parse_args(argv)

    if args.runs:
        pairs = _runs(Path(args.runs[0]), Path(args.runs[1]))
    elif len(args.paths) == 2:
        pairs = [(Path(args.paths[0]).stem, Path(args.paths[0]), Path(args.paths[1]))]
    else:
        parser.error("нужны OUT REF или --runs OUT_DIR INPUTS_DIR")

    total = {kind: KindScore() for kind in KINDS}
    dump = {}
    for name, produced, reference in pairs:
        comparison = compare_symbols(produced, reference)
        print(f"=== {name}", flush=True)
        print(comparison.report(), flush=True)
        for kind, score in comparison.scores.items():
            total[kind].add(score)
        dump[name] = {kind: vars(score) for kind, score in comparison.scores.items()}
    if len(pairs) > 1:
        print("\n=== итого")
        print(SymbolComparison(total, {}, {}).report())
    dump["_total"] = {kind: vars(score) for kind, score in total.items()}
    if args.json:
        Path(args.json).write_text(json.dumps(dump, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
