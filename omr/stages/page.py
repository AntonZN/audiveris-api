"""Стадия 1 — найти страницу: четырёхугольник, который надо выпрямить.

Два независимых оценщика:

* **по контуру** — классический «сканер документов»: самый большой
  четырёхугольный контур на кадре. Работает, когда лист контрастирует с фоном;
* **по нотным линейкам** — границы блока нот. Левые концы всех линеек лежат на
  одной прямой, правые — на другой, верх и низ задают крайние линейки.

Второй оценщик для нашей задачи лучше первого, и вот почему. Нас интересует не
лист бумаги, а прямоугольник, в который вписаны станы: именно он должен стать
прямоугольным, и именно на него надо потратить все пиксели. Он же переживает
случаи, где контурный оценщик бессилен: лист без полей в кадре, разворот книги,
белая бумага на белом столе. Контур остаётся запасным вариантом.

Если не сработал ни один — возвращаем весь кадр и НЕ warp'аем: пайплайн не имеет
права ухудшать картинку из-за неуверенной догадки.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from omr import geometry, staff
from omr.config import PipelineConfig


@dataclass
class PageQuad:
    """Найденный четырёхугольник в координатах поданного изображения."""

    quad: np.ndarray            # (4,2) tl, tr, br, bl
    source: str                 # "staff" | "contour" | "frame"
    confidence: float           # 0..1
    notes: dict = field(default_factory=dict)

    @property
    def is_identity(self) -> bool:
        return self.source == "frame"


def full_frame(shape: tuple[int, ...]) -> PageQuad:
    height, width = shape[:2]
    quad = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32
    )
    return PageQuad(quad, "frame", 0.0)


def detect_page(
    gray: np.ndarray,
    staves: list[staff.Staff],
    config: PipelineConfig,
    lines: list[staff.StaffLine] | None = None,
) -> PageQuad:
    """Выбрать четырёхугольник страницы. `staves` и `lines` — результат `staff.analyse`."""
    candidate = _from_staves(gray, staves, config, lines or [])
    if candidate is not None:
        return candidate
    candidate = _from_contour(gray, config)
    if candidate is not None:
        return candidate
    return full_frame(gray.shape)


# ----------------------------------------------------------------------------------
# Оценка по нотным линейкам
# ----------------------------------------------------------------------------------

def _from_staves(
    gray: np.ndarray,
    staves: list[staff.Staff],
    config: PipelineConfig,
    all_lines: list[staff.StaffLine],
) -> PageQuad | None:
    if len(staves) < config.min_staves_for_quad:
        return None

    lines = [line for s in staves for line in s.lines]
    centre = np.array([
        float(np.mean([line.x0 + line.x1 for line in lines]) / 2),
        float(np.mean([line.y_mid for line in lines])),
    ])
    # Боковые края — по ВНЕШНИМ концам линеек (см. geometry.fit_edge_line):
    # усреднение здесь систематически срезает музыку.
    left = geometry.fit_edge_line(np.array([line.left for line in lines]), centre)
    right = geometry.fit_edge_line(np.array([line.right for line in lines]), centre)
    block_width = float(np.median([line.length for line in lines]))
    # Верх и низ — по самим крайним линейкам: это цельные ломаные через всю
    # страницу, тут достаточно устойчивой регрессии. Крайней считаем и линейку,
    # которая в стан НЕ собралась (см. `stray_extent`): рамка по станам иначе
    # отрезает целую систему.
    upper, lower = stray_extent(staves, all_lines, block_width)
    first = upper if upper is not None else staves[0].top
    last = lower if lower is not None else staves[-1].bottom
    top = geometry.fit_line_robust(np.stack([first.xs, first.ys], axis=1))
    bottom = geometry.fit_line_robust(np.stack([last.xs, last.ys], axis=1))

    block_height = abs(staves[-1].bottom.y_mid - staves[0].top.y_mid) or block_width
    interline = float(np.median([s.interline for s in staves]))

    # Боковой отступ — не меньше того, НАСКОЛЬКО САМИ КОНЦЫ ЛИНЕЕК РАСХОДЯТСЯ.
    # Смысл прямой: если концы разъехались на N px, значит край известен нам с
    # точностью ±N, и оставить меньше N слака — гарантированно срезать музыку у
    # тех систем, что уходят дальше всех. На изогнутой странице этот разброс
    # доходит до десятков пикселей, и фиксированные 6% его не покрывают.
    spread = max(
        _outer_spread(left, np.array([line.left for line in lines])),
        _outer_spread(right, np.array([line.right for line in lines])),
    )
    margin_x = max(config.quad_margin * block_width, 3 * interline, spread)
    margin_y = max(config.quad_margin * block_height, 4 * interline)
    # Поле НЕ ограничиваем краем кадра, хотя за кадром музыки нет: пробовали
    # (2026-09-10), и слегка наклонная линия края, продлённая до угла кадра,
    # «упиралась» в край — поле обнулялось и срезало акколаду и ключи левее
    # начала линеек (Lieder-превью: голос и фортепиано слились в одну партию).
    # Добивка пустотой за краем стоит лишь чуть мельче страницы у движка.
    left = geometry.offset_away_from(left, centre, margin_x)
    right = geometry.offset_away_from(right, centre, margin_x)
    # Сверху втрое больше: там заголовок и обозначение темпа, которые homr читает
    # отдельным OCR — срезав их, мы потеряем метаданные, а не только красоту.
    # Лишние поля движку не мешают, срезанный заголовок — мешает.
    top = geometry.offset_away_from(top, centre, margin_y * 3.0)
    bottom = geometry.offset_away_from(bottom, centre, margin_y)

    try:
        quad = geometry.order_quad(
            np.array([
                geometry.intersect(top, left),
                geometry.intersect(top, right),
                geometry.intersect(bottom, right),
                geometry.intersect(bottom, left),
            ])
        )
    except ValueError:
        return None

    # Порог площади для «нотной» оценки заметно ниже, чем для контурной: блок нот
    # — это заведомо не весь кадр, а на снимке разворота или листа издалека он
    # честно занимает считаные проценты. Как раз этот случай мы и спасаем.
    if not _plausible(quad, gray.shape, config, min_area=config.min_staff_quad_area_ratio):
        return None

    # Уверенность растёт с числом станов и падает, если концы линеек плохо ложатся
    # на прямую. Разброс меряется в межлинейных интервалах — величина без
    # размерности, сравнимая между снимками любого разрешения. Большой разброс
    # означает либо систему с отступом, либо сильный изгиб: край проведён по
    # внешним точкам и музыку не срежет, но и доверять ему как измерению нельзя.
    residual = _residual(left, np.array([line.left for line in lines])) / max(interline, 1.0)  # noqa: E501
    confidence = min(1.0, len(staves) / 8.0) * float(np.clip(1.5 - residual / 4.0, 0.2, 1.0))
    return PageQuad(
        quad,
        "staff",
        confidence,
        {"staves": len(staves), "interline": round(interline, 2),
         "разброс_концов": round(residual, 2)},
    )


def stray_extent(
    staves: list[staff.Staff], lines: list[staff.StaffLine], block_width: float
) -> tuple[staff.StaffLine | None, staff.StaffLine | None]:
    """Крайние длинные линейки выше первого и ниже последнего стана, не собравшиеся в стан.

    Найдено на фото листа под углом (эталон «Ученик чародея», фагот): у нижнего
    стана перспектива развела интервал, соседние линейки срослись в обрывки, и
    цепочка из пяти не собралась — промежутки вышли 13.5 px при интервале 9. Сами
    линейки при этом найдены, через всю ширину. Рамка шла по последнему
    СОБРАННОМУ стану и отрезала систему целиком: 9 тактов из 71, а страховка
    («станов стало меньше») молчала — на входе их тоже нашлось пять.

    Строить по таким линейкам рамку нельзя, а вот не отрезать их — можно и
    нужно. Берём только то, что похоже на линейку стана: длиной с блок нот,
    толщиной как у линеек (край листа и его тень — втрое толще) и не дальше
    одного шага между станами от крайнего стана. Ошибка тут безвредна: рамка
    станет выше на одну систему, а не срежет музыку.
    """
    if len(staves) < 2 or not lines:
        return None, None
    used = {id(line) for s in staves for line in s.lines}
    thickness = float(np.median([line.thickness for s in staves for line in s.lines]))
    interline = float(np.median([s.interline for s in staves]))
    spacing = float(np.median(np.diff([s.top.y_mid for s in staves])))
    reach = 1.1 * spacing
    first_top, last_bottom = staves[0].top.y_mid, staves[-1].bottom.y_mid

    upper = lower = None
    for line in lines:
        if (
            id(line) in used
            or line.length < 0.6 * block_width
            or line.thickness > 2.5 * thickness
        ):
            continue
        y = line.y_mid
        if last_bottom + interline < y <= last_bottom + reach:
            if lower is None or y > lower.y_mid:
                lower = line
        elif first_top - reach <= y < first_top - interline:
            if upper is None or y < upper.y_mid:
                upper = line
    return upper, lower


def _outer_spread(line: np.ndarray, points: np.ndarray) -> float:
    """Насколько далеко за проведённый край заходят самые крайние концы линеек."""
    a, b, c = line
    norm = float(np.hypot(a, b)) or 1.0
    signed = (points @ np.array([a, b]) + c) / norm
    return float(np.quantile(np.abs(signed), 0.95))


def _residual(line: np.ndarray, points: np.ndarray) -> float:
    a, b, c = line
    norm = float(np.hypot(a, b)) or 1.0
    return float(np.median(np.abs(points @ np.array([a, b]) + c) / norm))


# ----------------------------------------------------------------------------------
# Оценка по контуру
# ----------------------------------------------------------------------------------

def _from_contour(gray: np.ndarray, config: PipelineConfig) -> PageQuad | None:
    """Самый большой выпуклый четырёхугольник на кадре.

    Два способа выделить лист — по границе (Canny) и по яркости (Оцу). Первый
    ловит белый лист на светлом столе, второй — лист на тёмном фоне и «письмо в
    рамке» из скриншотов. Берём тот, что дал больший правдоподобный контур.
    """
    height, width = gray.shape[:2]
    scale = 800 / max(width, 1)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) \
        if scale < 1 else gray.copy()
    blurred = cv2.GaussianBlur(small, (5, 5), 0)

    edges = cv2.Canny(blurred, 40, 120)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)
    _, bright = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))

    best: PageQuad | None = None
    for name, mask in (("canny", edges), ("otsu", bright)):
        quad = _largest_quad(mask)
        if quad is None:
            continue
        back = quad / (scale if scale < 1 else 1.0)
        if not _plausible(back, gray.shape, config, min_area=config.min_quad_area_ratio):
            continue
        confidence = 0.5 * geometry.quad_area(back) / (width * height)
        if best is None or confidence > best.confidence:
            best = PageQuad(geometry.order_quad(back), "contour", confidence, {"mask": name})
    return best


def _largest_quad(mask: np.ndarray) -> np.ndarray | None:
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        perimeter = cv2.arcLength(contour, True)
        for epsilon in (0.02, 0.03, 0.05):
            approx = cv2.approxPolyDP(contour, epsilon * perimeter, True)
            if len(approx) == 4 and cv2.isContourConvex(approx):
                return approx.reshape(4, 2).astype(np.float32)
    return None


def _plausible(
    quad: np.ndarray, shape: tuple[int, ...], config: PipelineConfig, min_area: float
) -> bool:
    """Отсеять вырожденные и «во весь кадр» четырёхугольники."""
    height, width = shape[:2]
    frame_area = float(width * height)
    area = geometry.quad_area(quad)
    if not (min_area * frame_area <= area <= config.max_quad_area_ratio * frame_area):
        return False
    if not geometry.is_convex(quad):
        return False
    target_w, target_h = geometry.quad_size(quad)
    if min(target_w, target_h) < 50:
        return False
    # Слишком «сплюснутый» четырёхугольник — почти наверняка ошибка детектора.
    return 0.15 <= target_w / target_h <= 6.0
