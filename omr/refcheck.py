"""Сверка распознавания с эталонным MusicXML: по станам, с диагностикой ключа.

    python -m omr.refcheck tests/images -o out/          # прогнать и сверить всё
    python -m omr.refcheck score.pdf -o out/             # один файл
    python -m omr.refcheck --compare out.musicxml ref.mxl   # сверить готовый выход

Эталон ищется рядом со входом по имени (`.musicxml`, `.xml`, `.mxl`). Вход без
эталона тоже прогоняется — видно структуру (партии, станы, такты).

Чем это отличается от `omr/score.py` (одна последовательность нот на весь файл) и
зачем понадобилось: жалобы шли на гранд-стан и басовый ключ, а там ошибка
живёт в СТРУКТУРЕ — какой стан к какой партии отнесён, звучат ли руки
одновременно, не прочитан ли басовый стан в скрипичном ключе. Одна общая цифра
этого не показывает. Поэтому:

* **seq по станам** — каждый стан эталона сопоставляется лучшему стану выхода
  (Левенштейн по высотам в порядке звучания). Стан, ушедший не в ту партию,
  виден как провал одной строки, а не как размазанный минус;
* **сдвиг ключа** — если мешок ступеней стана совпадает лучше после сдвига на N
  ступеней, это ключ прочитан не тот (басовый как скрипичный — сдвиг 12);
* **global** — все ноты вместе в порядке «такт, доля в такте». Время внутри
  такта берётся ДОЛЕЙ его длины, так что ошибка длительности в одной руке не
  сдвигает всё остальное во второй, а вот рука, уехавшая в другой такт или
  выложенная после другой руки, — сдвигает. Это и есть «звучат одновременно».

Две особенности эталонов, без которых цифры врут (набор `tests/images`):

* повторы в эталоне бывают РАЗВЁРНУТЫ (исполнительский порядок: у Моцарта такты
  9-16 эталона — это повтор 1-8). Если в эталоне нет знаков повтора, а в
  выходе есть, выход разворачивается;
* эталон бывает ФРАГМЕНТОМ страницы (у Шуберта — только №15 из трёх пьес на
  листе). Поэтому рядом со строгой метрикой считается `fragment`: лишняя музыка
  в начале и в конце выхода не штрафуется. Строгая остаётся — она ловит мусор.

Ударные (`tests/images/drum`): ноты без высоты (`<unpitched>`). Если они есть в
эталоне, все метрики считаются по ключу «место на стане + форма головки»
(`_drum_key`), а отдельно — совпадение только по месту (`position_f1`).
"""

from __future__ import annotations

import argparse
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_DEGREE = {"C": 0, "D": 1, "E": 2, "F": 3, "G": 4, "A": 5, "B": 6}
REFERENCE_SUFFIXES = (".musicxml", ".xml", ".mxl")


# ----------------------------------------------------------------------------------
# Чтение MusicXML
# ----------------------------------------------------------------------------------

@dataclass
class Note:
    onset: float     # номер такта + доля такта
    midi: int        # у ноты без высоты (ударные) -1
    degree: int      # ступень: октава*7 + буква; у ударных — место на стане (display-step)
    part: int
    staff: int
    head: str = ""   # форма головки: "" обычная, "x", "diamond"… — у ударных это инструмент
    unpitched: bool = False


@dataclass
class Score:
    notes: list[Note]
    clefs: dict = field(default_factory=dict)     # (партия, стан) -> Counter("G2", "F4")
    measures: list[int] = field(default_factory=list)
    staves: list[int] = field(default_factory=list)
    has_repeats: bool = False

    @property
    def parts(self) -> int:
        return len(self.measures)


def load_root(path: Path) -> ET.Element:
    """Корень MusicXML из `.musicxml`/`.xml` или сжатого `.mxl`, без пространств имён."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            name = None
            if "META-INF/container.xml" in archive.namelist():
                container = ET.fromstring(archive.read("META-INF/container.xml"))
                for element in container.iter():
                    if element.tag.endswith("rootfile"):
                        name = element.get("full-path")
                        break
            if name is None:
                name = next(n for n in archive.namelist()
                            if n.endswith((".xml", ".musicxml")) and not n.startswith("META"))
            root = ET.fromstring(archive.read(name))
    else:
        root = ET.parse(path).getroot()
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return root


def unfold_order(measures: list[ET.Element]) -> list[int]:
    """Порядок исполнения тактов: повторы и вольты развёрнуты (D.C./D.S. — нет)."""
    info = []
    current: set[int] = set()
    for measure in measures:
        forward = backward = closes = False
        endings = set(current)
        for bar in measure.findall("barline"):
            repeat = bar.find("repeat")
            if repeat is not None:
                forward |= repeat.get("direction") == "forward"
                backward |= repeat.get("direction") == "backward"
            ending = bar.find("ending")
            if ending is not None:
                numbers = {int(x) for x in (ending.get("number") or "").replace(" ", "").split(",")
                           if x.isdigit()}
                if ending.get("type") == "start":
                    current = numbers or {1}
                    endings = set(current)
                else:
                    closes = True
        info.append((forward, backward, endings))
        if closes:
            current = set()

    order: list[int] = []
    start, passage, done = 0, 1, set()
    index, guard = 0, 0
    while index < len(info) and guard < 20 * len(info) + 100:
        guard += 1
        forward, backward, endings = info[index]
        if forward and index != start:
            start, passage = index, 1
        if endings and passage not in endings:
            index += 1
            continue
        order.append(index)
        if backward and index not in done:
            done.add(index)
            passage += 1
            index = start
            continue
        if backward or (endings and 1 not in endings):
            start, passage = index + 1, 1
        index += 1
    return order


def read(path: Path, unfold: bool = False) -> Score:
    root = load_root(path)
    score = Score(notes=[], has_repeats=root.find(".//barline/repeat") is not None)
    for part_index, part in enumerate(root.findall("part")):
        divisions = 1.0
        staves = {1}
        written = part.findall("measure")
        measures = [written[i] for i in unfold_order(written)] if unfold else written
        score.measures.append(len(measures))
        for measure_index, measure in enumerate(measures):
            position = longest = last_onset = 0.0
            pending: list[tuple[float, Note]] = []
            for element in measure:
                if element.tag == "attributes":
                    try:
                        value = float((element.findtext("divisions") or "").strip())
                        if value > 0:
                            divisions = value
                    except ValueError:
                        pass
                    count = (element.findtext("staves") or "").strip()
                    if count.isdigit():
                        staves.update(range(1, int(count) + 1))
                    for clef in element.findall("clef"):
                        number = int(clef.get("number") or 1)
                        staves.add(number)
                        name = (clef.findtext("sign") or "?") + (clef.findtext("line") or "")
                        score.clefs.setdefault((part_index, number), Counter())[name] += 1
                elif element.tag == "backup":
                    position -= float(element.findtext("duration") or 0)
                elif element.tag == "forward":
                    position += float(element.findtext("duration") or 0)
                    longest = max(longest, position)
                elif element.tag == "note":
                    if element.find("grace") is not None or element.find("cue") is not None:
                        continue
                    duration = float(element.findtext("duration") or 0)
                    if element.find("chord") is not None:
                        onset = last_onset
                    else:
                        onset = last_onset = position
                        position += duration
                    longest = max(longest, position)
                    staff = int((element.findtext("staff") or "1").strip() or 1)
                    staves.add(staff)
                    head = _head(element.findtext("notehead"))
                    pitch = element.find("pitch")
                    unpitched = element.find("unpitched")
                    if pitch is not None:
                        step = pitch.findtext("step") or "C"
                        octave = int(pitch.findtext("octave") or 4)
                        alter = int(round(float(pitch.findtext("alter") or 0)))
                        midi = 12 * (octave + 1) + _SEMITONE[step] + alter
                        pending.append((onset, Note(0.0, midi, octave * 7 + _DEGREE[step],
                                                    part_index, staff, head)))
                    elif unpitched is not None:
                        # Без display-step нота стоит на средней линии (B4 скрипичного).
                        step = unpitched.findtext("display-step") or "B"
                        octave = int(unpitched.findtext("display-octave") or 4)
                        pending.append((onset, Note(0.0, -1, octave * 7 + _DEGREE[step],
                                                    part_index, staff, head, True)))
            for onset, note in pending:
                note.onset = measure_index + (onset / longest if longest > 0 else 0.0)
                score.notes.append(note)
        score.staves.append(len(staves))
    return score


# ----------------------------------------------------------------------------------
# Метрики
# ----------------------------------------------------------------------------------

def levenshtein(candidate, reference, free_ends: bool = False) -> int:
    """Левенштейн на numpy, строка за строкой (вставки — через накопленный минимум).

    `free_ends`: лишнее в начале и в конце КАНДИДАТА бесплатно — эталон фрагмент.
    """
    a, b = np.asarray(candidate), np.asarray(reference)
    if len(b) == 0:
        return 0 if free_ends else len(a)
    if len(a) == 0:
        return len(b)
    if len(a) < len(b) and not free_ends:
        a, b = b, a
    columns = np.arange(len(b) + 1)
    previous = columns.copy()
    best = int(previous[-1])
    for row, item in enumerate(a, start=1):
        current = np.empty(len(b) + 1, dtype=np.int64)
        current[0] = 0 if free_ends else row
        current[1:] = np.minimum(previous[1:] + 1, previous[:-1] + (b != item))
        previous = np.minimum.accumulate(current - columns) + columns
        best = min(best, int(previous[-1]))
    return best if free_ends else int(previous[-1])


def accuracy(candidate, reference, free_ends: bool = False) -> float:
    if not len(reference):
        return 0.0
    return 1.0 - levenshtein(candidate, reference, free_ends) / len(reference)


def _head(text: str | None) -> str:
    name = (text or "").strip().lower()
    if name == "normal":
        return ""
    return "x" if name == "cross" else name


# Коды головок для ключа ударных; незнакомая форма — последний код.
_HEADS = ("", "x", "circle-x", "diamond", "triangle", "slash", "square", "circle-dot")


def _pitch_key(note: Note) -> int:
    return note.midi


def _drum_key(note: Note) -> int:
    """Ударный «звук»: место на стане плюс форма головки.

    Ровно так Audiveris сам различает инструменты (drum-set.xml: pitch-position +
    motif). Номера инструментов из MusicXML сравнивать нельзя: MuseScore пишет в
    каждый файл всю установку, Audiveris называет их по-своему. Нота с высотой
    идёт сюда же по своей ступени — так обычная партия рядом с ударными
    (фортепиано в `metal-drum`) и выход homr, который ударных не знает и пишет
    их нотами с высотой, сравнимы с эталоном.
    """
    code = _HEADS.index(note.head) if note.head in _HEADS else len(_HEADS)
    return note.degree * 16 + code


def _sequence(notes: list[Note], key=_pitch_key) -> list[int]:
    return [key(n) for n in sorted(notes, key=lambda n: (round(n.onset, 4), key(n)))]


def _bag_f1(a: Counter, b: Counter) -> float:
    common = sum((a & b).values())
    if not common:
        return 0.0
    precision, recall = common / sum(a.values()), common / sum(b.values())
    return 2 * precision * recall / (precision + recall)


def _clef_shift(candidate: list[Note], reference: list[Note]) -> tuple[int, float, float]:
    """Сдвиг в ступенях, после которого ступени стана совпадают лучше всего."""
    target = Counter(n.degree for n in reference)
    base = _bag_f1(Counter(n.degree for n in candidate), target)
    best = (0, base)
    for shift in range(-16, 17):
        value = _bag_f1(Counter(n.degree + shift for n in candidate), target)
        if value > best[1] + 1e-9:
            best = (shift, value)
    return best[0], best[1], base


@dataclass
class StaffRow:
    reference: tuple[int, int]
    reference_clef: str
    reference_notes: int
    candidate: tuple[int, int] | None
    candidate_clef: str
    candidate_notes: int
    accuracy: float
    shift: int
    shift_f1: float
    base_f1: float


@dataclass
class Comparison:
    parts: tuple[int, int]
    staves: tuple[list[int], list[int]]
    measures: tuple[list[int], list[int]]
    notes: tuple[int, int]
    pitch_f1: float
    pitch_recall: float
    strict: float        # вся партитура, порядок звучания
    fragment: float      # то же, эталон как фрагмент выхода
    unfolded: bool
    staves_rows: list[StaffRow]
    unmatched: list[tuple[tuple[int, int], int]]
    # Эталон с ударными: всё выше считается по ключу «место + головка», а здесь —
    # только по месту на стане, без формы головки (None, если ударных нет).
    position_f1: float | None = None
    unpitched: tuple[int, int] = (0, 0)   # нот без высоты: выход / эталон

    def report(self) -> str:
        drum = self.position_f1 is not None
        lines = [
            f"партий {self.parts[0]}/{self.parts[1]}  станов {self.staves[0]}/{self.staves[1]}  "
            f"тактов {self.measures[0]}/{self.measures[1]}  нот {self.notes[0]}/{self.notes[1]}"
            + ("  (повторы выхода развёрнуты)" if self.unfolded else ""),
            f"{'место+головка' if drum else 'высоты'} F1 {self.pitch_f1 * 100:5.1f}%  "
            f"полнота {self.pitch_recall * 100:5.1f}%   "
            f"global {self.strict * 100:6.1f}%  fragment {self.fragment * 100:6.1f}%",
        ]
        if drum:
            lines.append(f"ударные: без высоты {self.unpitched[0]}/{self.unpitched[1]}  "
                         f"место (без головки) F1 {self.position_f1 * 100:5.1f}%")
        for row in self.staves_rows:
            where = "  -  " if row.candidate is None else f"P{row.candidate[0] + 1}s{row.candidate[1]}"
            shift = (f"  ключ? сдвиг {row.shift:+d} ступ. ({row.base_f1 * 100:.0f}%->"
                     f"{row.shift_f1 * 100:.0f}%)") if row.shift else ""
            lines.append(
                f"   эталон P{row.reference[0] + 1}s{row.reference[1]} [{row.reference_clef:>8s}] "
                f"n={row.reference_notes:4d}  ->  {where} [{row.candidate_clef:>8s}] "
                f"n={row.candidate_notes:4d}  seq={row.accuracy * 100:6.1f}%{shift}"
            )
        if self.unmatched:
            lines.append(f"   лишние станы на выходе: {self.unmatched}")
        return "\n".join(lines)


def compare(candidate_path: Path, reference_path: Path) -> Comparison:
    reference = read(reference_path)
    unfold = not reference.has_repeats and read(candidate_path).has_repeats
    candidate = read(candidate_path, unfold=unfold)

    def by_staff(score: Score) -> dict:
        streams: dict = {}
        for note in score.notes:
            streams.setdefault((note.part, note.staff), []).append(note)
        return streams

    ours, theirs = by_staff(candidate), by_staff(reference)
    drum = any(n.unpitched for n in reference.notes)
    key = _drum_key if drum else _pitch_key
    # Стан эталона -> лучший стан выхода, жадно по точности, каждый не больше раза.
    pairs = sorted(
        ((accuracy(_sequence(o, key), _sequence(r, key), True), rk, ok)
         for rk, r in theirs.items() for ok, o in ours.items()),
        key=lambda item: item[0], reverse=True,
    )
    taken_reference, taken_candidate, mapping = set(), set(), {}
    for value, rk, ok in pairs:
        if rk in taken_reference or ok in taken_candidate:
            continue
        taken_reference.add(rk)
        taken_candidate.add(ok)
        mapping[rk] = (ok, value)

    def clef(score: Score, key) -> str:
        return ",".join(name for name, _ in score.clefs.get(key, Counter()).most_common(3))

    rows = []
    for rk in sorted(theirs):
        if rk in mapping:
            ok, value = mapping[rk]
            shift, shift_f1, base_f1 = _clef_shift(ours[ok], theirs[rk])
            rows.append(StaffRow(rk, clef(reference, rk), len(theirs[rk]), ok, clef(candidate, ok),
                                 len(ours[ok]), value, shift, shift_f1, base_f1))
        else:
            rows.append(StaffRow(rk, clef(reference, rk), len(theirs[rk]), None, "", 0,
                                 0.0, 0, 0.0, 0.0))

    ours_bag = Counter(key(n) for n in candidate.notes)
    theirs_bag = Counter(key(n) for n in reference.notes)
    ours_seq, theirs_seq = _sequence(candidate.notes, key), _sequence(reference.notes, key)
    position_f1 = None
    if drum:
        position_f1 = _bag_f1(Counter(n.degree for n in candidate.notes),
                              Counter(n.degree for n in reference.notes))
    return Comparison(
        parts=(candidate.parts, reference.parts),
        staves=(candidate.staves, reference.staves),
        measures=(candidate.measures, reference.measures),
        notes=(len(candidate.notes), len(reference.notes)),
        pitch_f1=_bag_f1(ours_bag, theirs_bag),
        pitch_recall=sum((ours_bag & theirs_bag).values()) / max(len(reference.notes), 1),
        strict=accuracy(ours_seq, theirs_seq),
        fragment=accuracy(ours_seq, theirs_seq, free_ends=True),
        unfolded=unfold,
        staves_rows=rows,
        unmatched=[(k, len(notes)) for k, notes in ours.items() if k not in taken_candidate],
        position_f1=position_f1,
        unpitched=(sum(n.unpitched for n in candidate.notes),
                   sum(n.unpitched for n in reference.notes)),
    )


# ----------------------------------------------------------------------------------
# Прогон
# ----------------------------------------------------------------------------------

def reference_for(source: Path) -> Path | None:
    for suffix in REFERENCE_SUFFIXES:
        candidate = source.with_suffix(suffix)
        if candidate.exists() and candidate != source:
            return candidate
    return None


def collect(inputs: list[str]) -> list[Path]:
    from omr.cli import INPUT_SUFFIXES

    wanted = set(INPUT_SUFFIXES) | {".pdf"}
    paths: list[Path] = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            paths += sorted(p for p in path.rglob("*")
                            if p.suffix.lower() in wanted and ".clean" not in p.suffixes
                            and ".pages" not in str(p))
        else:
            paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omr.refcheck")
    parser.add_argument("inputs", nargs="*")
    parser.add_argument("-o", "--output", default="omr-refcheck")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--compare", nargs=2, metavar=("OUT", "REF"),
                        help="только сверить готовый MusicXML с эталоном")
    args = parser.parse_args(argv)

    if args.compare:
        print(compare(Path(args.compare[0]), Path(args.compare[1])).report())
        return 0

    from omr.config import DEFAULT
    from omr.recognize import recognize

    sources = collect(args.inputs)
    if not sources:
        print("нечего сверять: входов не найдено", file=sys.stderr)
        return 1
    summary = []
    for source in sources:
        started = time.monotonic()
        result = recognize(source, Path(args.output) / source.stem, DEFAULT, timeout=args.timeout)
        seconds = time.monotonic() - started
        print(f"=== {source.name}  {seconds:.0f}s", flush=True)
        print(result.report(), flush=True)
        if result.musicxml is None:
            summary.append((source.name, None))
            continue
        reference = reference_for(source)
        if reference is None:
            print("   эталона нет — только структура", flush=True)
            continue
        comparison = compare(result.musicxml, reference)
        print(comparison.report(), flush=True)
        summary.append((source.name, comparison))

    print("\nитого:")
    for name, comparison in summary:
        if comparison is None:
            print(f"  {name:60s} НЕ РАСПОЗНАН")
        else:
            worst = min((row.accuracy for row in comparison.staves_rows), default=0.0)
            print(f"  {name:60s} global {comparison.strict * 100:6.1f}%  "
                  f"fragment {comparison.fragment * 100:6.1f}%  худший стан {worst * 100:6.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
