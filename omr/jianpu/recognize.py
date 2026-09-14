"""Цзянпу: снимки или PDF -> один MusicXML через движок jpeditor.

    python -m omr.jianpu.recognize score.pdf -o out/
    python -m omr.jianpu.recognize page1.jpg page2.jpg -o out/ --stem song

Нотный пайплайн (`omr/recognize.py`) сюда не подходит: вся его геометрия —
поворот, рамка страницы, dewarp — держится на нотных линейках, а у цзянпу их
нет, и каждая стадия честно отказалась бы. Общее у них — вход и выход: PDF
растрируется постранично тем же `stages.pdf`, страницы склеиваются тем же
`merge`.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from omr.engines import jianpu_engine
from omr.engines.homr_engine import EngineError
from omr.jianpu import voices
from omr.merge import MergeReport, merge
from omr.stages import load as load_stage
from omr.stages import pdf as pdf_stage

# Ширина растра страницы PDF для движка. Замер на tests/images/jianpu/synth, F1 высот:
# 1240 px — 89.0%, 1654 — 90.5%, 2480 — 91.0%, 3307 — 90.1%. Дальше движку не помогает.
PDF_WIDTH = 2480


@dataclass
class JianpuPage:
    number: int
    image: Path          # что уходит в движок
    origin: str          # «score.pdf, стр.2», «photo.jpg»
    stem: str = ""       # имя выхода страницы
    musicxml: Path | None = None
    error: str = ""


@dataclass
class JianpuResult:
    musicxml: Path | None
    pages: list[JianpuPage] = field(default_factory=list)
    engine_seconds: float = 0.0
    engine_log: Path | None = None
    merge_report: MergeReport | None = None
    # Причина отказа до движка (многоголосие); пусто — движок запускался.
    refused: str = ""

    @property
    def recognised(self) -> int:
        return sum(1 for page in self.pages if page.musicxml is not None)

    def report(self) -> str:
        if self.refused:
            return f"цзянпу: ОТКАЗ до движка — {self.refused}"
        lines = [f"движок цзянпу (jpeditor): страниц {len(self.pages)}, "
                 f"распознано {self.recognised}, {self.engine_seconds:.1f} с"]
        for page in self.pages:
            state = f"ОШИБКА — {page.error}" if page.error else "ок"
            lines.append(f"  стр.{page.number} ({page.origin}): {state}")
        report = self.merge_report
        if report and report.pages > 1:
            lines.append(f"  склейка: страниц {report.pages}, партий {report.parts}, "
                         f"тактов {report.measures}")
            lines += [f"    {note}" for note in report.notes]
        return "\n".join(lines)


def collect_pages(
    sources: list[Path],
    work_dir: Path,
    *,
    stem: str,
    max_pages: int | None = None,
    pdf_width: int = PDF_WIDTH,
) -> list[JianpuPage]:
    """Входы -> страницы-картинки, которые движок прочитает как есть."""
    pages: list[JianpuPage] = []
    for source in map(Path, sources):
        if pdf_stage.is_pdf(source):
            rendered = pdf_stage.rasterize(source, work_dir / source.stem, target_width=pdf_width,
                                           headroom=1.0, max_pages=max_pages)
            pages += [JianpuPage(0, page.image, f"{source.name}, стр.{page.number}")
                      for page in rendered]
        else:
            pages.append(JianpuPage(0, _readable(source, work_dir), source.name))
    for number, page in enumerate(pages, start=1):
        page.number = number
        page.stem = f"{stem}.p{number:02d}" if len(pages) > 1 else stem
    return pages


def refuse_multivoice(pages: list[JianpuPage]) -> str:
    """Причина отказа, если хоть одна страница многоголосная; пусто — можно в движок.

    Отказ на всю песню, а не на страницу: хор без одной страницы — та же негодная
    партитура, только короче.
    """
    for page in pages:
        report = voices.detect(page.image)
        if report.multi:
            return (f"многоголосие на стр.{page.number} ({report.summary()}): движок читает "
                    "строки голосов подряд как одну мелодию, хоры и дуэты не поддерживаются")
    return ""


def _readable(source: Path, work_dir: Path) -> Path:
    """Снимок так, как его надо отдать движку: HEIC и повёрнутые по EXIF — в PNG.

    sharp в движке EXIF-ориентацию не применяет: телефонный снимок с пометкой
    «повернуть» пришёл бы к нему боком.
    """
    if source.suffix.lower() in jianpu_engine.SUFFIXES and not _exif_rotated(source):
        return source
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / f"{source.stem}.upright.png"
    cv2.imwrite(str(target), load_stage.load_bgr(source))
    return target


def _exif_rotated(path: Path) -> bool:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.getexif().get(0x0112, 1) not in (0, 1)
    except Exception:  # noqa: BLE001 — не прочитали EXIF, значит и поворачивать нечего
        return False


def assemble(result: JianpuResult, target: Path) -> JianpuResult:
    """Страницы -> один MusicXML в `target`: одна страница как есть, несколько — склейкой.

    Нераспознанная страница в склейку не идёт, а остаётся в отчёте — так же
    поступает с PDF нотный пайплайн.
    """
    produced = [page.musicxml for page in result.pages if page.musicxml is not None]
    if not produced:
        return result
    if len(produced) == 1:
        if produced[0] != target:
            produced[0].replace(target)
            for page in result.pages:
                if page.musicxml == produced[0]:
                    page.musicxml = target
    else:
        result.merge_report = merge(produced, target)
    result.musicxml = target
    return result


def recognize(
    sources: Path | list[Path],
    output_dir: Path,
    *,
    stem: str | None = None,
    timeout: int = 300,
    max_pages: int | None = None,
    pdf_width: int = PDF_WIDTH,
) -> JianpuResult:
    """Распознать снимки/PDF одной песней: `<output_dir>/<stem>.musicxml`."""
    sources = [Path(sources)] if isinstance(sources, (str, Path)) else [Path(s) for s in sources]
    if not sources:
        raise ValueError("нечего распознавать: пустой список входов")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or sources[0].stem

    pages = collect_pages(sources, output_dir / f"{stem}.pages", stem=stem,
                          max_pages=max_pages, pdf_width=pdf_width)
    if not pages:
        return JianpuResult(None)
    refusal = refuse_multivoice(pages)
    if refusal:
        return JianpuResult(None, pages, refused=refusal)
    run = jianpu_engine.run_pages([(page.image, page.stem) for page in pages], output_dir,
                                  timeout=timeout, log_name=f"{stem}.engine.log")
    for page in pages:
        page.musicxml = run.outputs.get(page.stem)
        page.error = run.errors.get(page.stem, "")
    result = JianpuResult(None, pages, run.seconds, run.log)
    return assemble(result, output_dir / f"{stem}.musicxml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omr.jianpu.recognize")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("-o", "--out", type=Path, required=True)
    parser.add_argument("--stem")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--pdf-width", type=int, default=PDF_WIDTH)
    args = parser.parse_args(argv)
    try:
        result = recognize(args.inputs, args.out, stem=args.stem, timeout=args.timeout,
                           pdf_width=args.pdf_width)
    except EngineError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(result.report())
    print(f"MusicXML: {result.musicxml}" if result.musicxml else "MusicXML не получен")
    return 0 if result.musicxml else 1


if __name__ == "__main__":
    sys.exit(main())
