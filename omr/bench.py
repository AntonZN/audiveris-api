"""Замер: что даёт подготовка. `python -m omr.bench <папка> [--reference <папка>]`

Каждый снимок гоняется через движок ДВАЖДЫ — как есть и после пайплайна — и
результаты сравниваются с эталонным MusicXML (файл с тем же именем рядом со
снимком или в папке `--reference`). Без эталона выводятся только структурные
числа: сколько станов увидел детектор, сколько тактов и нот выдал движок.

Смысл в том, чтобы любое изменение в пайплайне подтверждалось числом, а не
ощущением «вроде стало ровнее».
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from omr import score
from omr.cli import collect
from omr.config import DEFAULT, PipelineConfig
from omr.recognize import recognize


@dataclass
class Row:
    name: str
    variant: str
    staves: int
    measures: int
    notes: int
    accuracy: float | None
    seconds: float
    error: str = ""


def reference_for(image: Path, directory: Path | None) -> Path | None:
    for folder in filter(None, (directory, image.parent)):
        for suffix in (".musicxml", ".xml"):
            candidate = folder / f"{image.stem}{suffix}"
            if candidate.exists():
                return candidate
    return None


def run_one(
    image: Path, output: Path, config: PipelineConfig, raw: bool, timeout: int
) -> Row:
    variant = "как есть" if raw else "пайплайн"
    folder = output / ("raw" if raw else "prepared")
    started = time.monotonic()
    try:
        result = recognize(image, folder, config, timeout=timeout, skip_prepare=raw)
    except Exception as exc:
        return Row(image.name, variant, 0, 0, 0, None, time.monotonic() - started,
                   error=f"{type(exc).__name__}: {exc}".split("\n")[0])
    if result.musicxml is None:
        reasons = [p.error or p.skipped for p in result.pages if p.error or p.skipped]
        return Row(image.name, variant, 0, 0, 0, None, time.monotonic() - started,
                   error="; ".join(reasons) or "движок не дал MusicXML")
    sequence = score.read(result.musicxml)
    # Для контрольного прогона показываем, сколько станов различимо на снимке КАК
    # ЕСТЬ (а не после разворота внутри пайплайна) — иначе колонка врёт ровно на
    # тех файлах, ради которых стадия orient и написана. Для многостраничного
    # входа суммируем по страницам.
    reports = [page.prepare for page in result.pages if page.prepare is not None]
    staves = sum(
        (report.staves_as_shot if raw else report.staves_after) for report in reports
    )
    return Row(
        image.name, variant, staves=staves,
        measures=sequence.measures, notes=len(sequence),
        accuracy=None, seconds=time.monotonic() - started,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omr.bench")
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("-o", "--output", default="omr-bench")
    parser.add_argument("--reference", type=Path, default=None,
                        help="папка с эталонными MusicXML (по имени снимка)")
    parser.add_argument("--only", choices=["raw", "prepared"], default=None)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    output = Path(args.output)
    images = collect(args.inputs)
    if not images:
        print("нечего мерить: не найдено ни одной картинки", file=sys.stderr)
        return 1

    variants = [True, False] if args.only is None else [args.only == "raw"]
    rows: list[Row] = []
    for image in images:
        reference = reference_for(image, args.reference)
        expected = score.read(reference) if reference else None
        for raw in variants:
            row = run_one(image, output, DEFAULT, raw, args.timeout)
            if expected and not row.error:
                produced = score.read(
                    output / ("raw" if raw else "prepared") / f"{image.stem}.musicxml"
                )
                row.accuracy = score.accuracy(produced, expected)  # noqa: E501
            rows.append(row)
            print(_format(row), flush=True)

    print()
    print(_summary(rows))
    return 0


def _format(row: Row) -> str:
    if row.error:
        return f"{row.name:28s} {row.variant:9s}  ОШИБКА: {row.error}"
    accuracy = f"{row.accuracy * 100:5.1f}%" if row.accuracy is not None else "    -"
    return (f"{row.name:28s} {row.variant:9s}  станов={row.staves:2d} "
            f"тактов={row.measures:3d} нот={row.notes:4d} точность={accuracy} "
            f"{row.seconds:5.0f}s")


def _summary(rows: list[Row]) -> str:
    lines = ["итого по вариантам:"]
    for variant in ("как есть", "пайплайн"):
        subset = [r for r in rows if r.variant == variant]
        if not subset:
            continue
        ok = [r for r in subset if not r.error]
        scored = [r.accuracy for r in ok if r.accuracy is not None]
        average = f"{sum(scored) / len(scored) * 100:.1f}%" if scored else "-"
        lines.append(
            f"  {variant:9s} успехов {len(ok)}/{len(subset)}, "
            f"средняя точность {average}, "
            f"нот в среднем {sum(r.notes for r in ok) / max(len(ok), 1):.0f}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
