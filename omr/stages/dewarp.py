"""Стадия 3 — снять кривизну бумаги (фото книги у корешка).

Гомография выпрямляет ПЛОСКОСТЬ. Страница раскрытой книги плоскостью не
является: у корешка бумага уходит в цилиндр, строки выгибаются дугой, а
горизонтальный масштаб у сгиба сжимается. Никакой `warpPerspective` этого не
исправит.

Полноценный document dewarping восстанавливает 3D-поверхность листа. Нам этого
не нужно — у нот есть подсказка, которой нет у обычного текста: **нотные линейки
и есть линии сетки на поверхности**. Строка текста задаёт свою форму только по
базовой линии, а стан даёт пять параллельных кривых сразу, и они идут через всю
страницу. Поэтому:

1. берём каждую линейку как ломаную y(x);
2. считаем, насколько она в каждом X отклоняется от своей «ровной» высоты;
3. интерполируем это поле смещений между линейками по вертикали;
4. один `cv2.remap` — и страница плоская.

Плюс поправка по длине дуги вдоль X: у сгиба линейка длиннее своей хорды ровно
во столько раз, во сколько там сжат горизонтальный масштаб. Растягиваем X
обратно — и такты у корешка перестают быть уже, чем в середине страницы.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from omr import staff
from omr.config import PipelineConfig


@dataclass
class DewarpInfo:
    applied: bool
    reason: str
    amplitude_px: float = 0.0
    arc_ratio: float = 1.0
    lines_used: int = 0


def dewarp(
    image: np.ndarray, config: PipelineConfig
) -> tuple[np.ndarray, DewarpInfo]:
    """Выпрямить кривизну страницы по форме нотных линеек."""
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]

    # Через тот же `staff.analyse`, что и остальные стадии: он склеивает фрагменты
    # и дотягивает хвосты линеек. Для dewarp'а это принципиально — как раз у
    # корешка, где кривизна максимальна, детектор обрывает линейку, и на
    # необрезанных данных поле смещений просто не доходит до самого изогнутого
    # места.
    lines, _, _ = staff.analyse(
        gray,
        min_len_ratio=config.min_line_len_ratio,
        max_thickness_ratio=config.max_line_thickness_ratio,
        max_skew_deg=config.max_skew_deg,
    )
    # Только длинные линейки: короткий обрывок не несёт информации о форме
    # страницы, зато легко вносит в поле смещений выброс.
    lines = [line for line in lines if line.length > 0.35 * width]

    if len(lines) < config.min_lines_for_dewarp:
        return image, DewarpInfo(False, f"линеек мало ({len(lines)})", lines_used=len(lines))

    # ОБЩАЯ опора: участок X, который покрывают все взятые в расчёт линейки.
    # Без неё поле смещений разъезжается по вертикали. Пусть линейка A обрывается
    # на x=1600 и дальше держит своё смещение +100, а линейка B под ней тянется до
    # 1900 и там имеет +120: на правом поле между двумя соседними линейками
    # возникает скачок в 20 px, и ремап размазывает эту полосу в кашу. Поэтому
    # берём только линейки, покрывающие общий участок, и держим смещение за его
    # краями у ВСЕХ в одном и том же X.
    x_lo = float(np.quantile([line.x0 for line in lines], 0.5))
    x_hi = float(np.quantile([line.x1 for line in lines], 0.5))
    lines = [line for line in lines if line.x0 <= x_lo + 1 and line.x1 >= x_hi - 1]
    if len(lines) < config.min_lines_for_dewarp or x_hi - x_lo < width * 0.3:
        return image, DewarpInfo(
            False, f"нет общей опоры ({len(lines)} линеек)", lines_used=len(lines)
        )

    xs = np.arange(width, dtype=np.float32)
    # Профиль смещения каждой линейки: где она относительно своей средней высоты.
    anchors_y: list[float] = []
    offsets: list[np.ndarray] = []
    for line in lines:
        profile = _profile(line, xs, x_lo, x_hi, config)
        if profile is None:
            continue
        offset, centre = profile
        offsets.append(offset)
        anchors_y.append(centre)

    order = np.argsort(anchors_y)
    anchors = np.array(anchors_y, dtype=np.float32)[order]
    field = np.stack([offsets[i] for i in order])            # (n_lines, width)

    # Решение принимаем по ОБЩЕЙ форме, а не по самой кривой линейке. Настоящий
    # изгиб бумаги гнёт все линейки одинаково, поэтому медиана профилей его
    # сохраняет, а случайные ошибки детектора в ней гасятся. Без этого пайплайн
    # «выпрямлял» идеально плоские сканы, теряя резкость на ровном месте.
    common = np.median(field, axis=0)
    amplitude = float(np.max(np.abs(common)))
    if amplitude < config.min_dewarp_amplitude_px:
        return image, DewarpInfo(False, f"кривизна мала ({amplitude:.1f}px)",
                                 amplitude, lines_used=len(lines))
    if amplitude > config.max_dewarp_amplitude_ratio * height:
        return image, DewarpInfo(False, f"кривизна неправдоподобна ({amplitude:.1f}px)",
                                 amplitude, lines_used=len(lines))

    # Оставляем только линейки, которые гнутся ЗАОДНО с общей формой: остальные —
    # ошибки склейки, и тянуть по ним ремап значит рвать картинку.
    keep = [i for i in range(len(field)) if _agrees(field[i], common)]
    if len(keep) < config.min_lines_for_dewarp:
        return image, DewarpInfo(False, f"изгиб несогласован ({len(keep)} линеек)",
                                 amplitude, lines_used=len(keep))
    anchors, field = anchors[keep], field[keep]

    map_y = _interpolate_vertically(anchors, field, height, width)
    map_x, arc_ratio = _arc_length_map(field, width, height, config)

    dewarped = cv2.remap(
        image, map_x, map_y, interpolation=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255) if image.ndim == 3 else 255,
    )
    return dewarped, DewarpInfo(True, "ok", amplitude, arc_ratio, len(field))


def _agrees(profile: np.ndarray, common: np.ndarray, min_correlation: float = 0.3) -> bool:
    """Совпадает ли форма отдельной линейки с общей формой страницы."""
    a, b = profile - profile.mean(), common - common.mean()
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator < 1e-6:
        return False
    return float(a @ b / denominator) >= min_correlation


def _profile(
    line, xs: np.ndarray, x_lo: float, x_hi: float, config: PipelineConfig
) -> tuple[np.ndarray, float] | None:
    """Отклонение линейки от её ровной высоты во всех X, как гладкая кривая.

    Форму подгоняем многочленом низкой степени, а не просто сглаживаем. Причина
    физическая: изогнутая страница — это кусок цилиндра, её сечение описывается
    очень плавной кривой, и многочлена 4-й степени на неё хватает с запасом.
    Зато он полностью гасит шум трассировки на хвостах — без этого поле смещений
    получает выбросы в десятки пикселей ровно там, где данных меньше всего.

    Считается и подгоняется всё на ОБЩЕЙ опоре [x_lo, x_hi], одинаковой для всех
    линеек; за её краями X зажимается, то есть смещение держится постоянным — и
    держится у всех линеек в одном и том же месте.
    """
    inside = (xs >= x_lo) & (xs <= x_hi)
    if inside.sum() < 32:
        return None
    inside_x = xs[inside]
    values = np.interp(inside_x, line.xs, line.ys).astype(np.float64)
    centre = float(np.mean(values))

    degree = min(config.dewarp_polynomial_degree, max(1, int(inside.sum()) // 100))
    try:
        coefficients = np.polyfit(inside_x - x_lo, values - centre, degree)
    except (np.linalg.LinAlgError, ValueError):
        return None
    clamped = np.clip(xs, x_lo, x_hi) - x_lo
    return np.polyval(coefficients, clamped).astype(np.float32), centre


def _interpolate_vertically(
    anchors: np.ndarray, field: np.ndarray, height: int, width: int
) -> np.ndarray:
    """Разлить смещения линеек на все строки картинки.

    Между соседними линейками — линейно, выше верхней и ниже нижней — константой.
    Именно константой, а не продолжением наклона: линейная экстраполяция на полях
    страницы уезжает на сотни пикселей и рвёт картинку.

    Узлы (anchors) одни и те же для всех колонок, поэтому веса считаются один раз
    на всю картинку — цикла по 1920 колонкам здесь быть не должно.
    """
    rows = np.arange(height, dtype=np.float32)
    upper = np.clip(np.searchsorted(anchors, rows), 1, len(anchors) - 1)
    lower = upper - 1
    span = np.maximum(anchors[upper] - anchors[lower], 1e-6)
    weight = np.clip((rows - anchors[lower]) / span, 0.0, 1.0)[:, None]
    displacement = (1.0 - weight) * field[lower] + weight * field[upper]
    return (rows[:, None] + displacement).astype(np.float32)


def _arc_length_map(
    field: np.ndarray, width: int, height: int, config: PipelineConfig
) -> tuple[np.ndarray, float]:
    """Растянуть X так, чтобы вернуть сжатый у сгиба горизонтальный масштаб.

    Длина дуги линейки s(x) = ∫ sqrt(1 + (dy/dx)²) dx. Там, где линейка круто
    идёт вниз, на единицу экранного X приходится больше бумаги — значит,
    изображение там сжато. Обращаем s(x) и получаем сетку, равномерную по бумаге.
    """
    columns = np.arange(width, dtype=np.float32)
    slopes = np.gradient(field, axis=1)                       # dy/dx по каждой линейке
    density = np.sqrt(1.0 + np.median(slopes, axis=0) ** 2)   # медиана по линейкам
    arc = np.cumsum(density)
    ratio = float(arc[-1] / max(width, 1))
    if ratio < config.arc_correction_min_ratio:
        return np.tile(columns, (height, 1)), ratio
    arc = arc / arc[-1] * (width - 1)
    inverse = np.interp(columns, arc, columns).astype(np.float32)
    return np.tile(inverse, (height, 1)), ratio
