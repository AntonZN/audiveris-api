"""Сборка пайплайна: фотография -> ровный лист, готовый для движка OMR.

                     ФОТО
                       |
             [0] чтение + EXIF-ориентация
                       |
             [1] анализ геометрии на уменьшенной копии
                  /              \\
            контур страницы   нотные линейки
                  \\              /
                   четырёхугольник
                       |
             [2] гомография (перспектива)
                       |
             [3] dewarp по форме линеек (кривизна книги)
                       |
             [4] точный доворот по медианному углу линеек
                       |
             [5] выравнивание освещения + масштаб под движок
                       |
                  ЧИСТЫЙ ЛИСТ

Два принципа, которым подчинён весь модуль:

1. **Геометрию считаем на уменьшенной копии, применяем к оригиналу.** Каждый
   `warp`/`remap` — это интерполяция, то есть потеря резкости. Поэтому измерения
   идут по дешёвой уменьшенной картинке, а ремапов по полному разрешению ровно
   столько, сколько стадий реально сработало.

2. **Пайплайн не имеет права ухудшать.** Каждая стадия умеет отказаться
   («не уверен — не трогаю»), а в конце результат сверяется с входом по числу
   найденных станов. Стало хуже — отдаём простой вариант без геометрии.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from omr import staff
from omr.config import DEFAULT, PipelineConfig
from omr.debug import DebugWriter
from omr.geometry import scale_quad
from omr.stages import deskew as deskew_stage
from omr.stages import dewarp as dewarp_stage
from omr.stages import load as load_stage
from omr.stages import normalize as normalize_stage
from omr.stages import orient as orient_stage
from omr.stages import page as page_stage
from omr.stages import rectify as rectify_stage


@dataclass
class StageReport:
    """Что стадия сделала и почему. Основа читаемого отчёта пайплайна."""

    name: str
    applied: bool
    detail: str = ""
    metrics: dict = field(default_factory=dict)

    def __str__(self) -> str:
        mark = "+" if self.applied else "."
        extras = " ".join(f"{k}={v}" for k, v in self.metrics.items())
        return f"  [{mark}] {self.name:12s} {self.detail:34s} {extras}".rstrip()


@dataclass
class PrepareResult:
    """Готовая картинка плюс полный протокол того, как она получилась.

    `image` — ОДНОКАНАЛЬНАЯ (полутоновая): выравнивание освещения работает по
    яркости, а цвет для распознавания нот не несёт ничего. Движку это безразлично
    — `cv2.imread` всё равно вернёт ему три канала.
    """

    image: np.ndarray
    stages: list[StageReport]
    # Станов в ИСХОДНОЙ ориентации — то, что реально видит движок, если подать ему
    # снимок как есть. Отличается от `staves_before` на повёрнутой странице: там
    # `staves_before` считается уже после разворота.
    staves_as_shot: int
    staves_before: int
    staves_after: int
    interline_after: float | None
    reverted: bool = False
    source: Path | None = None

    def report(self) -> str:
        lines = [f"{self.source.name if self.source else '<array>'}: "
                 f"станов {self.staves_before} -> {self.staves_after}"
                 f"{'  (ОТКАТ: стало хуже)' if self.reverted else ''}"]
        lines += [str(s) for s in self.stages]
        return "\n".join(lines)


def prepare(
    source: Path | np.ndarray,
    config: PipelineConfig = DEFAULT,
    debug: DebugWriter | None = None,
) -> PrepareResult:
    """Прогнать снимок через весь пайплайн и вернуть картинку для движка."""
    debug = debug or DebugWriter(None)
    path = Path(source) if isinstance(source, (str, Path)) else None
    image = load_stage.load_bgr(path) if path else np.asarray(source)
    debug.image("input", image)

    reports: list[StageReport] = []

    # --- [1] Анализ геометрии на уменьшенной копии -------------------------------
    analysis, scale, lines, staves, interline = _analyse(image, config)
    staves_as_shot = staves_before = len(staves)
    reports.append(StageReport(
        "analyse", True, f"рабочая ширина {analysis.shape[1]}px",
        {"линеек": len(lines), "станов": staves_before,
         "интервал": round(interline or 0, 1),
         "перекос": f"{staff.median_angle(lines):+.2f}°"},
    ))
    debug.lines("analysis", analysis, lines, staves)

    # --- [1.5] Ориентация страницы -------------------------------------------------
    image, orientation = orient_stage.fix_orientation(
        image, analysis, staves_before, config
    )
    reports.append(StageReport("orient", orientation.rotated, orientation.reason))
    if orientation.rotated:
        debug.image("oriented", image)
        # После поворота вся геометрия считается заново — прежний разбор описывал
        # лежащую на боку страницу и для поиска рамки бесполезен.
        analysis, scale, lines, staves, interline = _analyse(image, config)
        staves_before = len(staves)
        debug.lines("analysis_oriented", analysis, lines, staves)

    # --- [2] Страница + гомография ------------------------------------------------
    quad = page_stage.detect_page(analysis, staves, config, lines)
    debug.quad("page_quad", analysis, quad.quad, f"{quad.source} conf={quad.confidence:.2f}")
    if quad.is_identity:
        reports.append(StageReport("page", False, "страница не выделена — кадр целиком"))
        current = image
    else:
        full_quad = scale_quad(quad.quad, 1.0 / scale)
        current, _ = rectify_stage.rectify(image, full_quad)
        reports.append(StageReport(
            "page+warp", True, f"по {quad.source}",
            {"уверенность": round(quad.confidence, 2),
             "было": f"{image.shape[1]}x{image.shape[0]}",
             "стало": f"{current.shape[1]}x{current.shape[0]}", **quad.notes},
        ))
        debug.image("rectified", current)

    # --- [3] Кривизна страницы ----------------------------------------------------
    # Работаем сразу по полному разрешению: после кропа страницы кадр уже
    # небольшой, а ремап по уменьшенной копии смысла не имеет — он всё равно
    # применяется к оригиналу.
    current, info = dewarp_stage.dewarp(current, config)
    reports.append(StageReport(
        "dewarp", info.applied, info.reason,
        {"амплитуда": f"{info.amplitude_px:.1f}px", "дуга": round(info.arc_ratio, 4),
         "линеек": info.lines_used},
    ))
    if info.applied:
        debug.image("dewarped", current)

    # --- [4] Точный доворот --------------------------------------------------------
    current, total_angle, passes = deskew_stage.deskew(current, config)
    reports.append(StageReport(
        "deskew", bool(passes), f"{total_angle:+.3f}°" if passes else "уже ровно",
        {"проходов": len(passes)},
    ))
    if passes:
        debug.image("deskewed", current)

    # --- [5] Освещение и масштаб ---------------------------------------------------
    _, _, staves_out, interline_out = _measure(current, config)
    flat = normalize_stage.flatten_illumination(current, config)
    scaled, factor, note = normalize_stage.scale_for_engine(flat, interline_out, config)
    reports.append(StageReport(
        "normalize", True, note or f"масштаб x{factor:.2f}",
        {"ширина": scaled.shape[1], "интервал": round((interline_out or 0) * factor, 1)},
    ))
    debug.image("output", scaled)

    # --- Страховка -----------------------------------------------------------------
    _, _, final_staves, final_interline = _measure(scaled, config)
    result = PrepareResult(
        scaled, reports, staves_as_shot, staves_before, len(final_staves),
        final_interline, source=path,
    )
    if config.safety_check and staves_before >= config.min_staves_for_quad:
        if len(final_staves) < config.safety_min_ratio * staves_before:
            fallback = _minimal(image, config)
            _, _, fb_staves, fb_interline = _measure(fallback, config)
            reports.append(StageReport(
                "safety", True, "геометрия ухудшила результат — откат",
                {"было": staves_before, "после пайплайна": len(final_staves),
                 "после отката": len(fb_staves)},
            ))
            debug.image("output_reverted", fallback)
            result = PrepareResult(
                fallback, reports, staves_as_shot, staves_before, len(fb_staves),
                fb_interline, reverted=True, source=path,
            )
    return result


def _minimal(image: np.ndarray, config: PipelineConfig) -> np.ndarray:
    """Запасной путь без геометрии: только освещение и масштаб."""
    _, _, _, interline = _measure(image, config)
    flat = normalize_stage.flatten_illumination(image, config)
    scaled, _, _ = normalize_stage.scale_for_engine(flat, interline, config)
    return scaled


def _analyse(
    image: np.ndarray, config: PipelineConfig
) -> tuple[np.ndarray, float, list, list, float | None]:
    """Разбор геометрии с подбором рабочего разрешения.

    Уменьшение до `analysis_width` экономит время, но если лист занимает малую
    часть кадра, межлинейное расстояние на уменьшенной копии падает до 2-3 px и
    линейки перестают детектироваться вовсе. Поэтому: не нашли станов — пробуем
    вдвое подробнее, вплоть до исходного разрешения.
    """
    width = config.analysis_width
    best: tuple = ()
    while True:
        small, scale = load_stage.downscale_to_width(image, width)
        gray = load_stage.to_gray(small)
        lines, staves, interline = staff.analyse(
            gray,
            min_len_ratio=config.min_line_len_ratio,
            max_thickness_ratio=config.max_line_thickness_ratio,
            max_skew_deg=config.max_skew_deg,
        )
        if not best or len(staves) > len(best[3]):
            best = (gray, scale, lines, staves, interline)
        if len(staves) >= config.analysis_enough_staves:
            break
        if scale >= 1.0 or width >= config.max_analysis_width:
            break
        width = min(width * 2, config.max_analysis_width, image.shape[1])
    return best


def _measure(
    image: np.ndarray, config: PipelineConfig
) -> tuple[np.ndarray, list, list, float | None]:
    """Замер «сколько станов видно» — для отчёта, масштаба и страховки.

    Тем же подбором разрешения, что и `_analyse`: иначе на кадре, где лист мал,
    сюда возвращается мусорный интервал (детектор цепляет случайные штрихи), и
    стадия normalize по нему ужимает страницу в ничто.
    """
    gray, scale, lines, staves, interline = _analyse(image, config)
    if not staves:
        # Без единого собранного стана интервалу верить нельзя — пусть normalize
        # ориентируется только на ширину кадра.
        return gray, lines, staves, None
    return gray, lines, staves, interline / max(scale, 1e-6)
