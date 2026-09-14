"""Замер движка цзянпу на эталонах: вход и MusicXML рядом, как у `omr.refcheck`.

    python -m omr.jianpu.bench tests/images/jianpu/synth -o out/jianpu-bench
    python -m omr.jianpu.bench tests/images/jianpu/synth -o out/ --pdf-width 1240

Все входы уходят в движок одним запуском (модель грузится один раз), затем
каждый сверяется `refcheck.compare`.

Октаву в цзянпу лист не задаёт — это договорённость (см. `melody.py`), поэтому
рядом с F1 высот считается F1 с поправкой на октаву. Сдвиг ровно на ±7 ступеней —
разные договорённости у эталона и движка, а не ошибка чтения, и прятать это
нельзя: колонка «окт» показывает, где такое случилось.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

from omr.engines import jianpu_engine
from omr.engines.homr_engine import EngineError
from omr.jianpu.recognize import (PDF_WIDTH, JianpuResult, assemble, collect_pages,
                                  refuse_multivoice)
from omr.refcheck import compare, reference_for

INPUT_SUFFIXES = jianpu_engine.SUFFIXES | {".pdf", ".heic", ".heif"}


def collect(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            paths += sorted(p for p in path.rglob("*")
                            if p.suffix.lower() in INPUT_SUFFIXES and ".pages" not in str(p))
        else:
            paths.append(path)
    return [path for path in paths if reference_for(path) is not None]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omr.jianpu.bench")
    parser.add_argument("inputs", nargs="+", help="файлы или папки; берутся входы с эталоном рядом")
    parser.add_argument("-o", "--out", type=Path, default=Path("omr-jianpu-bench"))
    parser.add_argument("--pdf-width", type=int, default=PDF_WIDTH)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    sources = collect(args.inputs)[: args.limit or None]
    if not sources:
        print("нечего мерить: нет входов с эталоном рядом", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)

    songs, refused = [], 0
    for index, source in enumerate(sources):
        stem = f"{index:03d}_{source.stem}"
        pages = collect_pages([source], args.out / f"{stem}.pages", stem=stem,
                              pdf_width=args.pdf_width)
        # Тот же отказ, что в API: иначе замер мерил бы то, что клиенту не уходит.
        refusal = refuse_multivoice(pages)
        if refusal:
            print(f"{source.name:28s} ОТКАЗ: {refusal}")
            refused += 1
            continue
        songs.append((source, stem, pages))
    everything = [(page.image, page.stem) for _, _, pages in songs for page in pages]
    try:
        run = jianpu_engine.run_pages(everything, args.out, timeout=60 + 10 * len(everything))
    except EngineError as exc:
        print(exc, file=sys.stderr)
        return 1

    rows = []
    for source, stem, pages in songs:
        for page in pages:
            page.musicxml = run.outputs.get(page.stem)
            page.error = run.errors.get(page.stem, "")
        result = assemble(JianpuResult(None, pages), args.out / f"{stem}.musicxml")
        if result.musicxml is None:
            print(f"{source.name:28s} НЕ РАСПОЗНАН: {'; '.join(p.error for p in pages if p.error)}")
            rows.append((source, None))
            continue
        comparison = compare(result.musicxml, reference_for(source))
        first = comparison.staves_rows[0] if len(comparison.staves_rows) == 1 else None
        octave = first is not None and abs(first.shift) == 7
        tolerant = first.shift_f1 if octave else comparison.pitch_f1
        rows.append((source, (comparison, tolerant, octave)))
        print(f"{source.name:28s} нот {comparison.notes[0]:4d}/{comparison.notes[1]:4d}  "
              f"тактов {comparison.measures[0][0] if comparison.measures[0] else 0:3d}/"
              f"{comparison.measures[1][0] if comparison.measures[1] else 0:3d}  "
              f"F1 {comparison.pitch_f1 * 100:5.1f}  с октавой {tolerant * 100:5.1f}"
              f"{' (окт)' if octave else '      '}  global {comparison.strict * 100:5.1f}")

    measured = [value for _, value in rows if value is not None]
    print(f"\nвходов {len(rows) + refused}, отказ {refused}, распознано {len(measured)}, "
          f"движок {run.seconds:.1f} с")
    if measured:
        f1 = [comparison.pitch_f1 for comparison, _, _ in measured]
        tolerant = [value for _, value, _ in measured]
        exact = sum(1 for comparison, _, _ in measured if comparison.strict == 1.0)
        print(f"F1 высот {statistics.mean(f1) * 100:.1f}%  с поправкой на октаву "
              f"{statistics.mean(tolerant) * 100:.1f}%  медиана {statistics.median(tolerant) * 100:.1f}%  "
              f"без ошибок {exact}  сдвиг октавы {sum(1 for *_, octave in measured if octave)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
