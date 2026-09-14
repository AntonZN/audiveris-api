"""Одноголосная мелодия: общая модель для цзянпу и эталонного MusicXML.

Модель описывает ровно то, что нарисовано на листе: одна `Note` — одна цифра
(или один «0»). Длительность, которую цзянпу не пишет одним знаком (половинная
с восьмой), заранее режется на знаки под лигой, и в эталонном MusicXML лежит
так же. Иначе сверка по нотам (`omr/refcheck.py`) штрафовала бы распознавание за
правильно прочитанную лигу.

Октава в цзянпу относительная: точки над и под цифрой отсчитываются от тоники
«средней» октавы, и абсолютную высоту с листа не прочитать. Договорённость одна
на весь проект — та, что у движка распознавания jpeditor (`jpPitch`): тоника без
точек стоит в 4-й октаве, а при букве тоники B (1=B, 1=Bb) — в 3-й. То есть
1=C — C4, 1=G — G4, 1=Bb — Bb3. Правило движка, а не своё, потому что октаву
лист не задаёт, а переписывать выход движка ради другой договорённости значит
держать ещё одно место, где можно ошибиться.

У jianpu-ly правило третье (буква тоники G–B — 3-я октава); поправку на него
делает сверка в `synth.py`, в модели её нет.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from functools import reduce

STEPS = "CDEFGAB"
SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_SHARPS = "FCGDAEB"
_FLATS = "BEADGCF"

# Длительность в четвертях -> (префикс jianpu-ly, точка, число тире).
_NOTE_GLYPHS = {
    Fraction(4): ("", "", 3),
    Fraction(3): ("", "", 2),
    Fraction(2): ("", "", 1),
    Fraction(3, 2): ("", ".", 0),
    Fraction(1): ("", "", 0),
    Fraction(3, 4): ("q", ".", 0),
    Fraction(1, 2): ("q", "", 0),
    Fraction(3, 8): ("s", ".", 0),
    Fraction(1, 4): ("s", "", 0),
    Fraction(3, 16): ("d", ".", 0),
    Fraction(1, 8): ("d", "", 0),
}
# Паузу тире не продлевают: половинная пауза в цзянпу — «0 0», а не «0 -».
_REST_GLYPHS = {d: g for d, g in _NOTE_GLYPHS.items() if g[2] == 0}

_TYPES = {
    Fraction(4): "whole", Fraction(2): "half", Fraction(1): "quarter",
    Fraction(1, 2): "eighth", Fraction(1, 4): "16th", Fraction(1, 8): "32nd",
    Fraction(1, 16): "64th",
}

# Размеры, в которых пишут цзянпу. Такт другой длины у источника — почти
# всегда ошибка ритма (у Anthology такты 9/16 и 21/16 — огрехи их распознавания),
# рисовать такое незачем.
ALLOWED_METERS = {(n, 4) for n in range(1, 7)} | {(n, 8) for n in (3, 6, 9, 12)}

_CJK = re.compile(r"[㐀-鿿]")


class Unsupported(ValueError):
    """Мелодию нельзя честно нарисовать цзянпу — источник пропускается.

    Текст начинается с категории до двоеточия: по ней считается сводка.
    """


@dataclass
class Note:
    duration: Fraction                # в четвертях
    step: str = ""                    # "" — пауза
    alter: int = 0
    octave: int = 4
    tie_start: bool = False
    tie_stop: bool = False
    lyrics: dict[int, str] = field(default_factory=dict)   # куплет -> слог

    @property
    def is_rest(self) -> bool:
        return not self.step

    @property
    def midi(self) -> int:
        return 12 * (self.octave + 1) + SEMITONE[self.step] + self.alter

    @property
    def diatonic(self) -> int:
        return self.octave * 7 + STEPS.index(self.step)


@dataclass
class Measure:
    notes: list[Note]
    time: tuple[int, int] | None = None   # размер, если объявлен в этом такте
    key: int | None = None                # квинты, если объявлены в этом такте
    pickup: bool = False                  # затакт

    @property
    def length(self) -> Fraction:
        return sum((n.duration for n in self.notes), Fraction(0))


@dataclass
class Melody:
    title: str
    measures: list[Measure]

    def notes(self):
        for measure in self.measures:
            yield from measure.notes

    def verses(self) -> list[int]:
        return sorted({verse for note in self.notes() for verse in note.lyrics})


# ----------------------------------------------------------------------------------
# Тональность
# ----------------------------------------------------------------------------------

def key_alter(step: str, fifths: int) -> int:
    """Альтерация ступени, которую даёт ключевой знак."""
    if fifths > 0 and step in _SHARPS[:fifths]:
        return 1
    if fifths < 0 and step in _FLATS[:-fifths]:
        return -1
    return 0


def tonic(fifths: int) -> tuple[str, int]:
    """Мажорная тоника тональности: буква и альтерация (цзянпу пишет 1= по мажору)."""
    step = STEPS[(4 * fifths) % 7]
    return step, key_alter(step, fifths)


def tonic_name(fifths: int) -> str:
    step, alter = tonic(fifths)
    return step + {-1: "b", 0: "", 1: "#"}[alter]


def tonic_diatonic(fifths: int) -> int:
    """Тоника без октавных точек — по договорённости из заголовка модуля."""
    step, _ = tonic(fifths)
    octave = 3 if step == "B" else 4
    return octave * 7 + STEPS.index(step)


# ----------------------------------------------------------------------------------
# Чтение MusicXML
# ----------------------------------------------------------------------------------

def clean_syllable(text: str) -> str:
    # Anthology дописывает к слогу «nan» (пустое значение из pandas): «代nan».
    if _CJK.search(text):
        text = text.replace("nan", "")
    elif text.strip().lower() == "nan":
        text = ""
    # Пробелы, кавычки и служебные символы jianpu-ly в слоге ломают строку текста.
    return re.sub(r'[\s"\\\-_~|]', "", text)


def read_musicxml(root: ET.Element, title: str = "") -> Melody:
    """Мелодия из MusicXML (корень без пространств имён, см. `refcheck.load_root`)."""
    parts = root.findall("part")
    if len(parts) != 1:
        raise Unsupported(f"партии: {len(parts)}")
    divisions = 1
    measures = []
    for element in parts[0].findall("measure"):
        measure = Measure([])
        for child in element:
            if child.tag == "attributes":
                if child.findtext("divisions"):
                    divisions = int(float(child.findtext("divisions")))
                if int(child.findtext("staves") or 1) > 1:
                    raise Unsupported("станы: больше одного")
                time = child.find("time")
                if time is not None and time.findtext("beats"):
                    beats = time.findtext("beats").strip()
                    if not beats.isdigit():
                        raise Unsupported(f"размер: {beats}")
                    measure.time = (int(beats), int(time.findtext("beat-type")))
                fifths = child.findtext("key/fifths")
                if fifths is not None:
                    measure.key = int(fifths)
            elif child.tag in ("backup", "forward"):
                raise Unsupported("голоса: больше одного")
            elif child.tag == "note":
                note = _read_note(child, divisions)
                if note is not None:
                    measure.notes.append(note)
        measures.append(measure)
    if not title:
        title = (root.findtext("work/work-title") or root.findtext("movement-title") or "").strip()
    return Melody(title, measures)


def _read_note(element: ET.Element, divisions: int) -> Note | None:
    if element.find("grace") is not None or element.find("cue") is not None:
        return None
    if element.find("chord") is not None:
        raise Unsupported("аккорды")
    if element.find("time-modification") is not None:
        raise Unsupported("триоли")
    duration = Fraction(int(float(element.findtext("duration") or 0)), divisions)
    if duration <= 0:
        # У Anthology такие есть в каждой седьмой песне; нарисовать их нечем, и
        # в эталон они не идут, как и форшлаги.
        return None
    note = Note(duration)
    pitch = element.find("pitch")
    if pitch is not None:
        alter = float(pitch.findtext("alter") or 0)
        if alter != int(alter):
            raise Unsupported("микротоны")
        note.step = pitch.findtext("step")
        note.alter = int(alter)
        note.octave = int(pitch.findtext("octave"))
    elif element.find("rest") is None:
        raise Unsupported("ноты без высоты")
    for tie in element.findall("tie") + element.findall("notations/tied"):
        note.tie_start |= tie.get("type") == "start"
        note.tie_stop |= tie.get("type") == "stop"
    for lyric in element.findall("lyric"):
        text = clean_syllable("".join(t.text or "" for t in lyric.findall("text")))
        number = re.sub(r"\D", "", lyric.get("number") or "") or "1"
        if text:
            note.lyrics[int(number)] = text
    return note


# ----------------------------------------------------------------------------------
# Подготовка к рисованию
# ----------------------------------------------------------------------------------

def bar_length(meter: tuple[int, int]) -> Fraction:
    return Fraction(4 * meter[0], meter[1])


def meter_of(length: Fraction) -> tuple[int, int] | None:
    for beat_type in (4, 8):
        beats = length * beat_type / 4
        if beats.denominator == 1:
            return int(beats), beat_type
    return None


def pickup_duration(length: Fraction) -> str | None:
    """Длительность затакта одной нотой в записи LilyPond («8», «4»), если выражается."""
    value = Fraction(4) / length
    if value.denominator == 1 and value.numerator & (value.numerator - 1) == 0:
        return str(value.numerator)
    return None


def prepare(melody: Melody) -> Melody:
    """Привести мелодию к виду, который рисуется цзянпу однозначно (на месте)."""
    if not melody.measures or not any(m.notes for m in melody.measures):
        raise Unsupported("пусто: нет нот")
    melody.measures = [m for m in melody.measures if m.notes]
    pair_ties(melody)
    fix_meters(melody)
    if melody.measures[0].key is None:
        melody.measures[0].key = 0
    settle_octave(melody)
    split_glyphs(melody)
    return melody


def pair_ties(melody: Melody) -> None:
    """Лига остаётся только между соседними нотами одной высоты, с обоих концов.

    У источников бывает половина лиги. jianpu-ly рисует лигу по её началу, а
    эталон сливал бы звук по концу, и лист с эталоном разошлись бы.
    """
    notes = list(melody.notes())
    for current, following in zip(notes, notes[1:]):
        linked = (current.tie_start and following.tie_stop and not current.is_rest
                  and not following.is_rest and current.midi == following.midi)
        current.tie_start = following.tie_stop = linked
    if notes:
        notes[0].tie_stop = notes[-1].tie_start = False


def fix_meters(melody: Melody) -> None:
    """Размер каждого такта — по его фактической длине.

    Объявленный размер источника берётся, если такт ему соответствует. Короткий
    первый такт — затакт. Остальные такты получают размер по длине, и если такой
    размер цзянпу не пишут, мелодия пропускается.

    Короткий последний такт тоже получает свой размер: недописанный такт LilyPond
    отмечает «bar check failed», и отличить его от настоящей ошибки нельзя.
    """
    measures = melody.measures
    count = len(measures)
    meters: list[tuple[int, int] | None] = []
    current = None
    for measure in measures:
        current = measure.time or current
        fits = current is not None and bar_length(current) == measure.length
        meters.append(current if fits else None)

    def inferred(index: int) -> tuple[int, int]:
        meter = meter_of(measures[index].length)
        if meter not in ALLOWED_METERS:
            raise Unsupported(f"размер: такт {index + 1} длиной {measures[index].length} четв.")
        return meter

    for index in range(1, count):
        if meters[index] is None:
            meters[index] = inferred(index)
    if meters[0] is None:
        follow = meters[1] if count > 1 else None
        if (follow and measures[0].length < bar_length(follow)
                and pickup_duration(measures[0].length)):
            meters[0] = follow
            measures[0].pickup = True
        else:
            meters[0] = inferred(0)

    previous = None
    for measure, meter in zip(measures, meters):
        measure.time = meter if meter != previous else None
        previous = meter


def _keyed_notes(melody: Melody):
    fifths = 0
    for measure in melody.measures:
        if measure.key is not None:
            fifths = measure.key
        for note in measure.notes:
            yield note, fifths


def settle_octave(melody: Melody) -> None:
    """Сдвинуть мелодию на целые октавы так, чтобы октавных точек было меньше всего.

    Абсолютная октава источника в цзянпу не видна, а без сдвига песня в 1=G,
    записанная вокруг G4, вышла бы с точкой над каждой цифрой.
    """
    pitched = [(n.diatonic, tonic_diatonic(f)) for n, f in _keyed_notes(melody) if not n.is_rest]

    def cost(shift: int) -> tuple[int, int]:
        return sum(abs((d + 7 * shift - t) // 7) for d, t in pitched), abs(shift)

    shift = min(range(-4, 5), key=cost)
    for note in melody.notes():
        if not note.is_rest:
            note.octave += shift
    worst = max((abs((d + 7 * shift - t) // 7) for d, t in pitched), default=0)
    if worst > 2:
        raise Unsupported("октава: больше двух точек")


def _pieces(duration: Fraction, table: dict) -> list[Fraction]:
    if duration in table:
        return [duration]
    # Паузы длиннее четверти пишутся четвертными «0 0», а не «0.» с довеском.
    values = sorted((d for d in table if table is _NOTE_GLYPHS or d <= 1), reverse=True)
    pieces, rest = [], duration
    for value in values:
        while rest >= value:
            pieces.append(value)
            rest -= value
    if rest:
        raise Unsupported(f"длительность: {duration} четв.")
    return pieces


def split_glyphs(melody: Melody) -> None:
    """Разрезать длительности на знаки цзянпу; слог — на первый знак."""
    for measure in melody.measures:
        glyphs = []
        for note in measure.notes:
            pieces = _pieces(note.duration, _REST_GLYPHS if note.is_rest else _NOTE_GLYPHS)
            for index, duration in enumerate(pieces):
                last = index == len(pieces) - 1
                glyphs.append(Note(
                    duration, note.step, note.alter, note.octave,
                    tie_start=not note.is_rest and (note.tie_start if last else True),
                    tie_stop=not note.is_rest and (note.tie_stop if index == 0 else True),
                    lyrics=dict(note.lyrics) if index == 0 else {},
                ))
        measure.notes = glyphs
    for note in melody.notes():
        # LilyPond не даёт слога ноте под лигой — эталон должен совпадать с листом.
        if note.tie_stop or note.is_rest:
            note.lyrics = {}


# ----------------------------------------------------------------------------------
# Запись
# ----------------------------------------------------------------------------------

def to_jly(melody: Melody, header: list[str] = ()) -> str:
    """Текст для jianpu-ly: такт в строку, тональность и размер — там, где меняются."""
    lines = []
    if melody.title:
        lines.append("title=" + re.sub(r'["\\\n]', "", melody.title))
    lines.extend(header)
    fifths = 0
    for number, measure in enumerate(melody.measures, 1):
        row = []
        if measure.key is not None:
            fifths = measure.key
            row.append("1=" + tonic_name(fifths))
        if measure.time is not None:
            meter = f"{measure.time[0]}/{measure.time[1]}"
            if measure.pickup:
                meter += "," + pickup_duration(measure.length)
            row.append(meter)
        signs: dict[int, int] = {}
        for note in measure.notes:
            row.append(_glyph(note, fifths, signs, number))
            if note.tie_start:
                row.append("~")
        row.append("|")
        lines.append(" ".join(row))
    verses = melody.verses()
    for verse in verses:
        syllables = [note.lyrics.get(verse, '""') for note in melody.notes()
                     if not note.is_rest and not note.tie_stop]
        lines.append("L: " + (f"{verse}. " if len(verses) > 1 else "") + " ".join(syllables))
    return "\n".join(lines) + "\n"


def _glyph(note: Note, fifths: int, signs: dict[int, int], number: int) -> str:
    table = _REST_GLYPHS if note.is_rest else _NOTE_GLYPHS
    prefix, dot, dashes = table[note.duration]
    if note.is_rest:
        return f"{prefix}0{dot}"
    offset = note.diatonic - tonic_diatonic(fifths)
    degree, marks = offset % 7 + 1, offset // 7
    accidental = note.alter - key_alter(note.step, fifths)
    if accidental not in (-1, 0, 1):
        raise Unsupported("знаки: двойная альтерация")
    # Случайный знак действует до конца такта. Одна ступень с разными знаками в
    # одном такте потребовала бы бекара, а как его нарисует jianpu-ly, не проверено.
    if signs.setdefault(note.diatonic, accidental) != accidental:
        raise Unsupported(f"знаки: бекар в такте {number}")
    sign = {-1: "b", 0: "", 1: "#"}[accidental]
    octave = "'" * marks if marks > 0 else "," * -marks
    return f"{prefix}{sign}{degree}{octave}{dot}" + " -" * dashes


def note_type(duration: Fraction) -> tuple[str, int]:
    for dots, factor in ((0, Fraction(1)), (1, Fraction(3, 2)), (2, Fraction(7, 4))):
        if duration / factor in _TYPES:
            return _TYPES[duration / factor], dots
    raise Unsupported(f"длительность: {duration} четв.")


def to_musicxml(melody: Melody) -> bytes:
    divisions = reduce(math.lcm, (n.duration.denominator for n in melody.notes()), 1)
    root = ET.Element("score-partwise", version="3.1")
    ET.SubElement(ET.SubElement(root, "work"), "work-title").text = melody.title
    encoding = ET.SubElement(ET.SubElement(root, "identification"), "encoding")
    ET.SubElement(encoding, "software").text = "omr.jianpu"
    score_part = ET.SubElement(ET.SubElement(root, "part-list"), "score-part", id="P1")
    ET.SubElement(score_part, "part-name").text = "Melody"
    part = ET.SubElement(root, "part", id="P1")
    number = 0 if melody.measures[0].pickup else 1
    last = len(melody.measures) - 1
    for index, measure in enumerate(melody.measures):
        element = ET.SubElement(part, "measure", number=str(number))
        number += 1
        if measure.pickup:
            element.set("implicit", "yes")
        if index == 0 or measure.key is not None or measure.time is not None:
            attributes = ET.SubElement(element, "attributes")
            if index == 0:
                ET.SubElement(attributes, "divisions").text = str(divisions)
            if measure.key is not None:
                ET.SubElement(ET.SubElement(attributes, "key"), "fifths").text = str(measure.key)
            if measure.time is not None:
                time = ET.SubElement(attributes, "time")
                ET.SubElement(time, "beats").text = str(measure.time[0])
                ET.SubElement(time, "beat-type").text = str(measure.time[1])
            if index == 0:
                clef = ET.SubElement(attributes, "clef")
                ET.SubElement(clef, "sign").text = "G"
                ET.SubElement(clef, "line").text = "2"
        for note in measure.notes:
            _write_note(element, note, divisions)
        if index == last:
            barline = ET.SubElement(element, "barline", location="right")
            ET.SubElement(barline, "bar-style").text = "light-heavy"
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def _write_note(parent: ET.Element, note: Note, divisions: int) -> None:
    element = ET.SubElement(parent, "note")
    if note.is_rest:
        ET.SubElement(element, "rest")
    else:
        pitch = ET.SubElement(element, "pitch")
        ET.SubElement(pitch, "step").text = note.step
        if note.alter:
            ET.SubElement(pitch, "alter").text = str(note.alter)
        ET.SubElement(pitch, "octave").text = str(note.octave)
    ET.SubElement(element, "duration").text = str(int(note.duration * divisions))
    ties = [kind for kind, on in (("stop", note.tie_stop), ("start", note.tie_start)) if on]
    for kind in ties:
        ET.SubElement(element, "tie", type=kind)
    ET.SubElement(element, "voice").text = "1"
    type_name, dots = note_type(note.duration)
    ET.SubElement(element, "type").text = type_name
    for _ in range(dots):
        ET.SubElement(element, "dot")
    if ties:
        notations = ET.SubElement(element, "notations")
        for kind in ties:
            ET.SubElement(notations, "tied", type=kind)
    for verse, text in sorted(note.lyrics.items()):
        lyric = ET.SubElement(element, "lyric", number=str(verse))
        ET.SubElement(lyric, "syllabic").text = "single"
        ET.SubElement(lyric, "text").text = text


def sounding(melody: Melody, transpose=None) -> list[tuple[Fraction, int, Fraction]]:
    """(начало, MIDI, длительность) звучащих нот: знаки под лигой слиты в один звук.

    `transpose(fifths)` — сдвиг в полутонах для нот в данной тональности: чужой
    инструмент может читать октаву цзянпу по своей договорённости.
    """
    notes: list[tuple[Fraction, int, Fraction]] = []
    onset = Fraction(0)
    for note, fifths in _keyed_notes(melody):
        if not note.is_rest:
            midi = note.midi + (transpose(fifths) if transpose else 0)
            previous = notes[-1] if notes else None
            if (note.tie_stop and previous and previous[1] == midi
                    and previous[0] + previous[2] == onset):
                notes[-1] = (previous[0], midi, previous[2] + note.duration)
            else:
                notes.append((onset, midi, note.duration))
        onset += note.duration
    return notes
