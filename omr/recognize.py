"""Верхний уровень: фотография или PDF -> MusicXML.

Единый вход для всех форматов. PDF не выделен в отдельный маршрут: он просто
растеризуется постранично, и дальше каждая страница идёт тем же самым
пайплайном, что и обычный снимок. Это не косметика — Audiveris обрабатывает PDF
книгой целиком, и одна страница без нот обнуляет экспорт всей книги (см.
`omr/stages/pdf.py`). Постранично такая страница просто пропускается.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2

from omr.config import DEFAULT, PipelineConfig
from omr.debug import DebugWriter
from omr.engines import homr_engine
from omr.merge import MergeReport, merge
from omr.pipeline import PrepareResult, prepare
from omr.stages import load as load_stage
from omr.stages import pdf as pdf_stage
from omr.stages import spread as spread_stage


@dataclass
class PageResult:
    """Итог по одной странице (для картинки страница ровно одна)."""

    number: int
    source: Path
    origin: str = ""            # откуда взялась: "стр.3", "разворот, левая"…
    prepare: PrepareResult | None = None
    clean_image: Path | None = None
    musicxml: Path | None = None
    engine_seconds: float = 0.0
    # Что движок прочитал OCR-ом над верхним станом: там метрономная отметка.
    ocr_texts: list[str] = field(default_factory=list)
    skipped: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.musicxml is not None


@dataclass
class RecognizeResult:
    musicxml: Path | None
    pages: list[PageResult] = field(default_factory=list)
    merge_report: MergeReport | None = None

    @property
    def recognised(self) -> int:
        return sum(1 for page in self.pages if page.ok)

    @property
    def ocr_texts(self) -> list[str]:
        """Сырые строки OCR со всех страниц, по порядку и без повторов.

        Темп ищут по ним. Первая страница важнее остальных (отметка стоит в начале
        пьесы), поэтому порядок сохраняем как есть.
        """
        seen: list[str] = []
        for page in self.pages:
            for text in page.ocr_texts:
                if text not in seen:
                    seen.append(text)
        return seen

    def report(self) -> str:
        lines = []
        for page in self.pages:
            where = f" ({page.origin})" if page.origin else ""
            if page.skipped:
                lines.append(f"  стр.{page.number}{where}: пропущена — {page.skipped}")
            elif page.error:
                lines.append(f"  стр.{page.number}{where}: ОШИБКА — {page.error}")
            else:
                lines.append(
                    f"  стр.{page.number}{where}: ок, движок {page.engine_seconds:.0f}s"
                )
        if self.merge_report and self.merge_report.pages > 1:
            report = self.merge_report
            lines.append(
                f"  склейка: страниц {report.pages}, партий {report.parts}, "
                f"тактов {report.measures}"
                + (f", добито паузами {report.padded_measures}" if report.padded_measures else "")
            )
            lines += [f"    {note}" for note in report.notes]
        return "\n".join(lines)


def recognize(
    source: Path,
    output_dir: Path,
    config: PipelineConfig = DEFAULT,
    *,
    debug_dir: Path | None = None,
    timeout: int = 300,
    skip_prepare: bool = False,
    max_pages: int | None = None,
) -> RecognizeResult:
    """Распознать снимок или PDF.

    `skip_prepare=True` отдаёт движку страницу КАК ЕСТЬ — контрольный вариант для
    замеров (см. omr/bench.py). Пайплайн при этом всё равно отрабатывает: его
    отчёт нужен, чтобы в таблице было видно, сколько станов вообще различимо на
    снимке; на результат движка в этом режиме он не влияет.
    """
    source, output_dir = Path(source), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pages_dir = output_dir / f"{source.stem}.pages"
    if pdf_stage.is_pdf(source):
        rendered = pdf_stage.rasterize(
            source, pages_dir,
            target_width=config.target_width, max_pages=max_pages,
        )
        # Пометку листа несём в origin ТОЛЬКО ради разворотов: без разреза номер
        # страницы и так равен номеру листа, и «стр.3 (стр.3)» в логе — шум.
        sheets = [(page.image, f"лист {page.number}") for page in rendered]
    else:
        sheets = [(source, "")]

    # Разворот книги режем по корешку — и для PDF тоже: отсканированный разворот
    # встречается внутри PDF ровно так же, как на фотографии.
    plan: list[tuple[Path, list[tuple[Path, str]]]] = [
        (sheet, _split_spread(sheet, origin, pages_dir, config))
        for sheet, origin in sheets
    ]
    # Индекс в имени файла нужен, только когда страниц действительно несколько.
    # Считаем это отдельно для листов и для их половин: лист PDF остаётся
    # пронумерованным всегда, а единственная картинка получает индекс, лишь пока
    # она разрезана на половины. Если разрез потом откатится, лист снова станет
    # единственной страницей — и имя должно быть простым, без `.p01`.
    many_sheets = len(sheets) > 1
    split_anything = any(len(parts) > 1 for _, parts in plan)

    def run(number: int, item: tuple[Path, str], indexed: bool) -> PageResult:
        return _recognize_page(
            number, item[0], item[1], source, output_dir, config,
            debug_dir=debug_dir, timeout=timeout, skip_prepare=skip_prepare,
            stem=_page_stem(source, number, indexed),
        )

    results: list[PageResult] = []
    for sheet, parts in plan:
        produced = [
            run(len(results) + offset + 1, item, many_sheets or split_anything)
            for offset, item in enumerate(parts)
        ]

        # Половина разворота не распозналась — откатываемся к неразрезанному листу.
        # Разрез задуман как улучшение, и если он что-то сломал, лучше вернуться
        # к тому, что работало: половина партитуры хуже целой.
        if len(parts) > 1 and any(not page.ok and not page.skipped for page in produced):
            whole = run(len(results) + 1, (sheet, "разворот целиком (откат)"), many_sheets)
            if whole.ok:
                _discard(produced)
                produced = [whole]

        results.extend(produced)

    for number, page in enumerate(results, start=1):
        page.number = number

    produced = [page.musicxml for page in results if page.musicxml is not None]
    if not produced:
        # Ни одной страницы. Если движок при этом ПАДАЛ (а не просто не нашёл нот),
        # даём ему второй заход по странице как есть: подготовка иногда делает
        # хуже — перспектива на почти плоском листе, разгибание там, где гнуть
        # нечего. Для картинки такой второй заход есть и снаружи (homr на сыром
        # файле), а для PDF снаружи его быть не может: homr не читает PDF. Так что
        # честнее сделать его здесь — тогда лист PDF и снимок равны в правах.
        if not skip_prepare and any(page.error for page in results):
            retry = recognize(
                source, output_dir, config, debug_dir=debug_dir, timeout=timeout,
                skip_prepare=True, max_pages=max_pages,
            )
            if retry.musicxml is not None:
                for page in retry.pages:
                    page.origin = (page.origin + " (без подготовки)").strip()
                return retry
        return RecognizeResult(None, results)
    if len(produced) == 1:
        return RecognizeResult(produced[0], results)

    merged = output_dir / f"{source.stem}.musicxml"
    report = merge(produced, merged)
    return RecognizeResult(merged, results, report)


def _page_stem(source: Path, number: int, indexed: bool) -> str:
    return f"{source.stem}.p{number:02d}" if indexed else source.stem


def _discard(pages: list[PageResult]) -> None:
    """Убрать файлы отменённых страниц.

    Выход раздаётся как медиа, и брошенный там MusicXML от половины, которую мы
    в итоге не взяли, — это не мусор на диске, а подсунутый клиенту неверный
    файл. Удаляем всё, что она за собой оставила.
    """
    for page in pages:
        for artefact in (page.musicxml, page.clean_image):
            if artefact is not None:
                artefact.unlink(missing_ok=True)


def _split_spread(
    sheet: Path, origin: str, pages_dir: Path, config: PipelineConfig
) -> list[tuple[Path, str]]:
    """Развернуть один лист в страницы: разворот книги — в две, обычный — в одну."""
    image = load_stage.load_bgr(sheet)
    info = spread_stage.detect(image, config)
    if not info.is_spread or info.split_x is None:
        # Целый лист: номер страницы в отчёте и так его называет.
        return [(sheet, "")]

    pages_dir.mkdir(parents=True, exist_ok=True)
    halves = spread_stage.split(image, info.split_x)
    result: list[tuple[Path, str]] = []
    for index, (half, side) in enumerate(zip(halves, ("левая", "правая")), start=1):
        target = pages_dir / f"{sheet.stem}.half{index}.png"
        cv2.imwrite(str(target), half)
        label = f"{origin}, разворот/{side}" if origin else f"разворот/{side}"
        result.append((target, label))
    return result


def _recognize_page(
    number: int,
    image_path: Path,
    origin: str,
    source: Path,
    output_dir: Path,
    config: PipelineConfig,
    *,
    debug_dir: Path | None,
    timeout: int,
    skip_prepare: bool,
    stem: str,
) -> PageResult:
    page = PageResult(number=number, source=image_path, origin=origin)

    # Отладочные кадры кладём в подпапку страницы, когда имя страницы
    # индексировано — то есть ровно когда страниц несколько.
    indexed = stem != source.stem
    debug = DebugWriter(debug_dir / f"p{number:02d}" if debug_dir and indexed else debug_dir)
    page.prepare = prepare(image_path, config, debug)

    # Кадр, каким его увидит движок, сохраняем ДО решения о пропуске: именно на
    # пропущенных страницах и хочется потом посмотреть, что подготовка сделала с
    # геометрией — в архиве провалов это главный экспонат.
    clean = output_dir / f"{stem}.clean.png"
    cv2.imwrite(str(clean), page.prepare.image)
    page.clean_image = clean

    # Страница без единого стана — обложка, колофон, оборот. Гнать её через
    # движок незачем: он на ней либо упадёт, либо выдаст пустышку.
    if page.prepare.staves_after == 0 and page.prepare.staves_as_shot == 0:
        page.skipped = "нотных станов не найдено"
        return page

    try:
        outcome = homr_engine.run(
            image_path if skip_prepare else page.prepare.image,
            output_dir, timeout=timeout, stem=stem,
        )
    except homr_engine.EngineError as exc:
        page.error = str(exc).split("\n")[0]
        return page

    page.musicxml = outcome.musicxml
    page.engine_seconds = outcome.seconds
    page.ocr_texts = outcome.ocr_texts
    return page
