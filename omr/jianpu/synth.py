"""Синтетические эталоны цзянпу: мелодия -> лист цзянпу (PDF) + эталонный MusicXML.

    python -m omr.jianpu.synth essen:han1 -o tests/images/jianpu/synth --limit 20
    python -m omr.jianpu.synth anthology/lyrics-included/jiangsu1 -o out/ --png 150

Вход — MusicXML (`.musicxml`, `.xml`, `.mxl`), ABC (сборник целиком) или папки с
ними; `essen:han1` / `essen:han2` — китайские народные песни из корпуса Essen,
который лежит внутри music21.

Зачем. Готовых пар «лист цзянпу + эталонный MusicXML» в открытом доступе нет:
у датасета Anthology of Chinese Folk Songs выложены только MusicXML (сканы
закрыты авторским правом), GT jpeditor в репозиторий не входит, у OrpheusNet
данных нет вовсе. Поэтому пары делаются из символьной музыки: мелодия ->
текст jianpu-ly -> LilyPond.

Эталон не предполагается совпадающим с листом, а проверяется: MIDI, который
LilyPond строит из того же файла, что и PDF, сверяется с эталоном нота в ноту
(высота, начало, длительность). Пара, не прошедшая сверку, не пишется.

Нужны `lilypond` (`brew install lilypond`) и `jianpu-ly`
(`uv tool install jianpu-ly`); пути можно задать через LILYPOND и JIANPU_LY.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote

from omr.jianpu.melody import (Melody, Unsupported, prepare, read_musicxml, sounding,
                               to_jly, to_musicxml, tonic)
from omr.refcheck import load_root

SUFFIXES = (".musicxml", ".xml", ".mxl", ".abc")

# jianpu-ly на macOS всегда вставляет этот блок, а set-global-fonts из LilyPond 2.24+
# убран: без правки LilyPond пишет error и возвращает ненулевой код.
_FONT_BLOCK = re.compile(r"#\(define fonts\s*\(set-global-fonts.*?\)\s*\)\s*\)", re.S)


@dataclass
class Tools:
    jianpu_ly: str
    lilypond: str


@dataclass
class Style:
    """Вариации вёрстки: движок не должен привыкать к одному кеглю и одному листу."""
    staff_size: int
    bar_numbers: bool
    a5: bool
    # «1=F 2/4» строкой в левом верхнем углу, как в китайских сборниках, а не
    # дробью перед первым тактом.
    corner_meter: bool
    # Шрифт текста и цифр (None — шрифты LilyPond по умолчанию).
    font: str | None = None

    def header(self) -> list[str]:
        return ([] if self.bar_numbers else ["NoBarNums"]) + (["SeparateTimesig"] if self.corner_meter else [])


def pick_style(name: str, seed: str, fonts: list[str]) -> Style:
    rng = random.Random(f"{seed}:{name}")
    return Style(staff_size=rng.choice([17, 20, 23, 26]),
                 bar_numbers=rng.random() < 0.3,
                 a5=rng.random() < 0.2,
                 corner_meter=rng.random() < 0.5,
                 font=rng.choice([None, *fonts]))


def cjk_fonts() -> list[str]:
    """Китайские шрифты из списка, которые есть на этой машине (fontconfig)."""
    wanted = ["Songti SC", "STSong", "Heiti SC", "Hiragino Sans GB",
              "Noto Serif CJK SC", "Noto Sans CJK SC", "Source Han Serif SC", "Source Han Sans SC"]
    try:
        listed = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
    except OSError:
        return []
    families = {name.strip() for line in listed.splitlines() for name in line.split(",")}
    return [font for font in wanted if font in families]


def find_tools() -> Tools:
    jianpu_ly = os.environ.get("JIANPU_LY") or shutil.which("jianpu-ly")
    lilypond = os.environ.get("LILYPOND") or shutil.which("lilypond")
    if not jianpu_ly or not lilypond:
        sys.exit("нужны jianpu-ly (uv tool install jianpu-ly) и lilypond (brew install lilypond)")
    return Tools(jianpu_ly, lilypond)


# ----------------------------------------------------------------------------------
# Источники
# ----------------------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def iter_sources(inputs: list[str]):
    """(имя пары, загрузчик мелодии) — загрузка ленивая, чтобы ошибка была на источник."""
    for item in inputs:
        if item.startswith("essen:"):
            from music21 import common
            yield from _abc(Path(common.getCorpusFilePath()) / "essenFolksong" / f"{item[6:]}.abc")
            continue
        path = Path(item)
        files = (sorted(p for p in path.rglob("*") if p.suffix.lower() in SUFFIXES)
                 if path.is_dir() else [path])
        for file in files:
            if file.suffix.lower() == ".abc":
                yield from _abc(file)
            else:
                yield _slug(f"{file.parent.name}_{unquote(file.stem)}"), (lambda f=file: _from_musicxml(f))


def _from_musicxml(path: Path) -> Melody:
    stem = unquote(path.stem)
    # У Anthology в work-title служебное «页面_song_100», а название песни — в имени файла.
    title = re.sub(r"^\d+_", "", stem) if re.search(r"[㐀-鿿]", stem) else ""
    return read_musicxml(load_root(path), title)


def _abc(path: Path):
    from music21 import converter, stream
    from music21.musicxml.m21ToXml import GeneralObjectExporter

    opus = converter.parse(path)
    scores = list(opus.scores) if isinstance(opus, stream.Opus) else [opus]
    for index, score in enumerate(scores, 1):
        number = (score.metadata.number if score.metadata else None) or index

        def load(score=score):
            root = ET.fromstring(GeneralObjectExporter(score).parse())
            title = score.metadata.title if score.metadata and score.metadata.title else ""
            return read_musicxml(root, title)

        yield _slug(f"{path.stem}_{int(number):04d}"), load


# ----------------------------------------------------------------------------------
# Рисование и сверка
# ----------------------------------------------------------------------------------

def patch_ly(text: str, style: Style) -> str:
    text = _FONT_BLOCK.sub("", text)
    text = text.replace('% \\header { tagline="" }', '\\header { tagline="" }')
    text = re.sub(r"#\(set-global-staff-size \d+\)", f"#(set-global-staff-size {style.staff_size})", text)
    if style.a5:
        text = text.replace('% #(set-default-paper-size "a5" )', '#(set-default-paper-size "a5" )')
    if style.font:
        fonts = (f'\\paper {{ property-defaults.fonts.serif = "{style.font}" '
                 f'property-defaults.fonts.sans = "{style.font}" }}')
        text = text.replace("\\pointAndClickOff", "\\pointAndClickOff\n" + fonts, 1)
    return text


def jianpu_ly_octave(fifths: int) -> int:
    """jianpu-ly играет «1» с буквой тоники G–B в 3-й октаве, у нас в 3-й только B.

    Проверено по его MIDI на всех 15 тональностях (1=Cb звучит как B3 — это
    записанная Cb4, правило то же).
    """
    return -12 if tonic(fifths)[0] in "GA" else 0


def read_midi(path: Path) -> list[tuple[Fraction, int, Fraction]]:
    from music21 import midi

    midi_file = midi.MidiFile()
    midi_file.open(str(path))
    midi_file.read()
    midi_file.close()
    ticks = midi_file.ticksPerQuarterNote
    notes = []
    for track in midi_file.tracks:
        time, started = 0, {}
        for event in track.events:
            if event.isDeltaTime():
                time += event.time
            elif event.isNoteOn() and event.velocity > 0:
                started[(event.channel, event.pitch)] = time
            elif event.isNoteOff() or event.isNoteOn():
                start = started.pop((event.channel, event.pitch), None)
                if start is not None:
                    notes.append((Fraction(start, ticks), event.pitch, Fraction(time - start, ticks)))
    return sorted(notes)


def _first_difference(got: list, want: list) -> str:
    for index, (a, b) in enumerate(zip(got, want)):
        if a != b:
            return f"нота {index + 1}: лист {a[1]}@{a[0]}x{a[2]}, эталон {b[1]}@{b[0]}x{b[2]}"
    return f"нот на листе {len(got)}, в эталоне {len(want)}"


def render(melody: Melody, name: str, out: Path, tools: Tools, style: Style, png_dpi: int) -> list[Path]:
    jly = to_jly(melody, style.header())
    reference = to_musicxml(melody)
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        (work / f"{name}.jly").write_text(jly, encoding="utf-8")
        run = subprocess.run([tools.jianpu_ly, "--noStaff", f"{name}.jly"], cwd=work,
                             capture_output=True, text=True, timeout=120,
                             env=dict(os.environ, j2ly_sloppy_bars="1"))
        if run.returncode:
            raise Unsupported("jianpu-ly: " + (run.stderr.strip().splitlines() or ["?"])[-1][:160])
        (work / f"{name}.ly").write_text(patch_ly(run.stdout, style), encoding="utf-8")
        # cairo, а не ghostscript: тот встраивает китайский шрифт целиком (885 КБ
        # на лист против 45 КБ).
        command = [tools.lilypond, "-dno-point-and-click", "-dbackend=cairo", "--pdf"]
        if png_dpi:
            command += ["--png", f"-dresolution={png_dpi}"]
        run = subprocess.run(command + ["-o", name, f"{name}.ly"], cwd=work,
                             capture_output=True, text=True, timeout=300)
        problems = [line for line in run.stderr.splitlines()
                    if "error" in line or "bar check failed" in line]
        if run.returncode or problems:
            raise Unsupported("lilypond: " + (problems or ["код " + str(run.returncode)])[0][:160])
        got, want = read_midi(work / f"{name}.midi"), sounding(melody, jianpu_ly_octave)
        if got != want:
            raise Unsupported("сверка: " + _first_difference(got, want))
        written = []
        for page in sorted(work.glob(f"{name}.pdf")) + sorted(work.glob(f"{name}*.png")):
            target = out / page.name
            shutil.copyfile(page, target)
            written.append(target)
    (out / f"{name}.musicxml").write_bytes(reference)
    (out / f"{name}.jly").write_text(jly, encoding="utf-8")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omr.jianpu.synth",
                                     description="Пары «лист цзянпу (PDF) + эталонный MusicXML».")
    parser.add_argument("inputs", nargs="+", help="MusicXML/ABC, папки, essen:han1|han2")
    parser.add_argument("-o", "--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0, help="сколько пар сделать (0 — все)")
    parser.add_argument("--png", type=int, default=0, metavar="DPI",
                        help="ещё и PNG страниц (в git не попадают: *.png в .gitignore)")
    parser.add_argument("--min-notes", type=int, default=16)
    parser.add_argument("--max-measures", type=int, default=100)
    parser.add_argument("--seed", default="1", help="зерно вариаций вёрстки")
    args = parser.parse_args(argv)

    tools = find_tools()
    fonts = cjk_fonts()
    args.out.mkdir(parents=True, exist_ok=True)
    made, skipped, seen = 0, Counter(), set()
    for name, load in iter_sources(args.inputs):
        if args.limit and made >= args.limit:
            break
        if name in seen:
            name = f"{name}_{len(seen)}"
        seen.add(name)
        try:
            melody = prepare(load())
            notes = sum(1 for note in melody.notes() if not note.is_rest)
            if notes < args.min_notes:
                raise Unsupported(f"размер песни: нот {notes}")
            if len(melody.measures) > args.max_measures:
                raise Unsupported(f"размер песни: тактов {len(melody.measures)}")
            render(melody, name, args.out, tools, pick_style(name, args.seed, fonts), args.png)
        except Unsupported as error:
            skipped[str(error).split(":")[0]] += 1
            print(f"  [-] {name}: {error}")
            continue
        made += 1
        print(f"  [+] {name}: тактов {len(melody.measures)}, нот {notes}, "
              f"куплетов {len(melody.verses())}")
    print(f"готово: {made} пар в {args.out}; пропущено {sum(skipped.values())}"
          + "".join(f", {kind} {n}" for kind, n in skipped.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
