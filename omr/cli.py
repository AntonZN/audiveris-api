"""Командная строка: `python -m omr <фото> [ещё фото...]`.

    python -m omr photo.jpg -o out/ --debug
    python -m omr photos/ -o out/ --prepare-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

from omr import recognize
from omr.config import DEFAULT, PipelineConfig
from omr.debug import DebugWriter
from omr.engines.homr_engine import EngineError
from omr.pipeline import prepare
from omr.stages.load import UnreadableImage
from omr.stages.pdf import PdfUnavailable, is_pdf, rasterize

# PDF здесь наравне с картинками: он растеризуется постранично и идёт тем же
# пайплайном (см. omr/stages/pdf.py).
INPUT_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff",
                  ".heic", ".heif", ".pdf"}


def collect(inputs: list[str]) -> list[Path]:
    """Развернуть файлы и папки в список картинок."""
    paths: list[Path] = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            paths += sorted(
                p for p in path.rglob("*")
                if p.suffix.lower() in INPUT_SUFFIXES and ".clean" not in p.suffixes
            )
        else:
            paths.append(path)
    return paths


def _pages_of(path: Path, output: Path, config) -> list[tuple[int, Path]]:
    """Развернуть вход в страницы: PDF — постранично, разворот книги — пополам."""
    from omr.stages import load as load_stage
    from omr.stages import spread as spread_stage

    if is_pdf(path):
        sheets = [
            page.image
            for page in rasterize(
                path, output / f"{path.stem}.pages", target_width=config.target_width
            )
        ]
    else:
        sheets = [path]

    pages: list[tuple[int, Path]] = []
    for sheet in sheets:
        image = load_stage.load_bgr(sheet)
        info = spread_stage.detect(image, config)
        if not info.is_spread or info.split_x is None:
            pages.append((len(pages) + 1, sheet))
            continue
        directory = output / f"{path.stem}.pages"
        directory.mkdir(parents=True, exist_ok=True)
        for index, half in enumerate(spread_stage.split(image, info.split_x), start=1):
            target = directory / f"{sheet.stem}.half{index}.png"
            cv2.imwrite(str(target), half)
            pages.append((len(pages) + 1, target))
    return pages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omr", description="Подготовка фото нот и распознавание в MusicXML"
    )
    parser.add_argument("inputs", nargs="+", help="файлы или папки со снимками")
    parser.add_argument("-o", "--output", default="omr-out", help="куда складывать результат")
    parser.add_argument("--debug", action="store_true",
                        help="сохранять картинку после каждой стадии")
    parser.add_argument("--prepare-only", action="store_true",
                        help="только подготовить лист, движок не запускать")
    parser.add_argument("--raw", action="store_true",
                        help="отдать движку снимок как есть (контрольный прогон)")
    parser.add_argument("--timeout", type=int, default=300, help="таймаут движка, секунд")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="ограничить число страниц PDF")
    parser.add_argument("--analysis-width", type=int, default=DEFAULT.analysis_width)
    parser.add_argument("--target-width", type=int, default=DEFAULT.target_width)
    parser.add_argument("--no-safety", action="store_true",
                        help="не откатываться, даже если геометрия ухудшила результат")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = PipelineConfig(
        analysis_width=args.analysis_width,
        target_width=args.target_width,
        safety_check=not args.no_safety,
    )
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    failures = 0
    for path in collect(args.inputs):
        debug_dir = output / f"{path.stem}.debug" if args.debug else None
        try:
            if args.prepare_only:
                for number, image in _pages_of(path, output, config):
                    result = prepare(image, config, DebugWriter(debug_dir))
                    clean = output / f"{image.stem}.clean.png"
                    cv2.imwrite(str(clean), result.image)
                    print(result.report())
                    print(f"  -> {clean}")
            else:
                result = recognize(
                    path, output, config, debug_dir=debug_dir,
                    timeout=args.timeout, skip_prepare=args.raw,
                    max_pages=args.max_pages,
                )
                for page in result.pages:
                    if page.prepare is not None and not page.skipped:
                        print(page.prepare.report())
                print(result.report())
                if result.musicxml is None:
                    failures += 1
                    print(f"{path.name}: ничего не распознано", file=sys.stderr)
                else:
                    print(f"  -> {result.musicxml}")
        except (UnreadableImage, EngineError, PdfUnavailable) as exc:
            failures += 1
            print(f"{path.name}: ОШИБКА — {exc}", file=sys.stderr)
        print()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
