"""Детекция нотных линеек — общий примитив для всех стадий пайплайна.

Ключевая идея всей затеи: у нотного листа есть признак, которого нет у обычного
документа, — **пять длинных параллельных равноудалённых линеек**. Это готовая
«калибровочная сетка», нанесённая прямо на страницу. По ней можно:

* оценить перекос (стадия deskew),
* найти границы блока нот и, значит, страницы (стадия page),
* измерить кривизну бумаги у корешка (стадия dewarp),
* измерить межлинейное расстояние и выбрать финальный масштаб (стадия normalize).

Поэтому детектор здесь один, а стадии — его потребители.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


# eq=False: dataclass-евое сравнение полей упало бы на numpy-массивах
# ("truth value of an array is ambiguous"), а нам от сравнения нужна только
# идентичность объектов — по ней работает list.remove в merge_fragments.
@dataclass(eq=False)
class StaffLine:
    """Одна нотная линейка как ломаная y(x), а не как прямая.

    Именно ломаная: на фотографии книги линейка изогнута, и вся стадия dewarp
    держится на том, что мы храним её реальную форму.
    """

    xs: np.ndarray        # координаты X выборки (возрастают)
    ys: np.ndarray        # координата Y линейки в каждом xs
    thickness: float      # средняя толщина штриха, px

    @property
    def x0(self) -> float:
        return float(self.xs[0])

    @property
    def x1(self) -> float:
        return float(self.xs[-1])

    @property
    def length(self) -> float:
        return self.x1 - self.x0

    @property
    def y_mid(self) -> float:
        return float(np.median(self.ys))

    @property
    def left(self) -> np.ndarray:
        return np.array([self.xs[0], self.ys[0]], dtype=np.float32)

    @property
    def right(self) -> np.ndarray:
        return np.array([self.xs[-1], self.ys[-1]], dtype=np.float32)

    @property
    def angle_deg(self) -> float:
        """Наклон по прямой, проведённой через концы ломаной."""
        dx = self.x1 - self.x0
        if dx <= 0:
            return 0.0
        return float(np.degrees(np.arctan2(self.ys[-1] - self.ys[0], dx)))

    def y_at(self, x: float) -> float:
        """Y линейки в конкретном X (с зажимом на концах).

        Сравнивать линейки нужно именно в общем X, а не по медианному Y: на
        фотографии книги линейка выгибается на десятки пикселей, и две соседние
        по медиане могут «перепрыгнуть» друг друга.
        """
        return float(np.interp(x, self.xs, self.ys))

    @property
    def curvature_px(self) -> float:
        """Максимальное отклонение ломаной от хорды — «насколько выгнута»."""
        chord = np.interp(self.xs, [self.x0, self.x1], [self.ys[0], self.ys[-1]])
        return float(np.max(np.abs(self.ys - chord)))


# ----------------------------------------------------------------------------------
# Бинаризация
# ----------------------------------------------------------------------------------

def ink_mask(gray: np.ndarray) -> np.ndarray:
    """Маска «чернил» (255 = штрих) для геометрического анализа.

    ВАЖНО: эта маска нужна ТОЛЬКО нам, чтобы мерить геометрию. В движок уходит
    обычная полутоновая картинка — трансформерный OMR обучен на естественных
    изображениях и на бинаризованном входе работает заметно хуже.

    Адаптивный порог, а не Оцу: на фотографии освещение неравномерное, и
    глобальный порог либо съедает тонкие линейки в тени, либо заливает
    пересвеченный угол.
    """
    height, width = gray.shape[:2]
    block = max(15, (min(height, width) // 40) | 1)  # нечётное
    # НИКАКОЙ «чистки шума» изотропным ядром здесь быть не должно. Нотная линейка
    # печатается в 1-2 px, и открытие даже ядром 2x2 стирает самые тонкие из них —
    # ровно по одной линейке на стан, из-за чего пятёрки перестают собираться.
    # Длинные горизонтальные штрихи мы всё равно отфильтруем на следующем шаге
    # направленной морфологией, а она шум убирает без потерь.
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 12
    )


# ----------------------------------------------------------------------------------
# Оценка перекоса по профилю проекции
# ----------------------------------------------------------------------------------

def estimate_skew(mask: np.ndarray, max_deg: float = 12.0) -> float:
    """Угол перекоса в градусах (положительный = страница повёрнута по часовой).

    Классический projection profile: сдвигаем строки по X-зависимому смещению
    (сдвиг, а не поворот — он дешевле и для малых углов эквивалентен) и берём
    угол, при котором горизонтальная проекция самая «контрастная». Нотные линейки
    дают в профиле резкие пики, поэтому максимум дисперсии очень выражен.

    Два прохода: грубый с шагом 0.5° по всему диапазону, затем точный с шагом
    0.05° вокруг найденного — 24 + 20 дешёвых свёрток вместо 480.
    """
    small = _downscale(mask, 900)

    def sharpness(angle_deg: float) -> float:
        sheared = _shear_rows(small, angle_deg)
        profile = sheared.sum(axis=1, dtype=np.float64)
        # Сумма квадратов разностей соседних строк: растёт, когда линейки
        # укладываются каждая в свою строку, а не размазываются по нескольким.
        return float(np.sum(np.diff(profile) ** 2))

    coarse = np.arange(-max_deg, max_deg + 1e-9, 0.5)
    best = max(coarse, key=sharpness)
    fine = np.arange(best - 0.5, best + 0.5 + 1e-9, 0.05)
    return float(max(fine, key=sharpness))


def _shear_rows(mask: np.ndarray, angle_deg: float) -> np.ndarray:
    """Вертикальный сдвиг, компенсирующий наклон на angle_deg."""
    if abs(angle_deg) < 1e-6:
        return mask
    height, width = mask.shape[:2]
    matrix = np.array([[1.0, 0.0, 0.0], [-np.tan(np.radians(angle_deg)), 1.0, 0.0]])
    return cv2.warpAffine(mask, matrix, (width, height), flags=cv2.INTER_NEAREST)


def _downscale(image: np.ndarray, target_width: int) -> np.ndarray:
    if image.shape[1] <= target_width:
        return image
    scale = target_width / image.shape[1]
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


# ----------------------------------------------------------------------------------
# Детекция линеек
# ----------------------------------------------------------------------------------

def detect_lines(
    gray: np.ndarray,
    *,
    min_len_ratio: float = 0.08,
    max_thickness_ratio: float = 0.006,
    bands: int = 6,
    max_skew_deg: float = 12.0,
    mask: np.ndarray | None = None,
) -> list[StaffLine]:
    """Найти фрагменты нотных линеек и вернуть их как ломаные y(x).

    Линейку выделяет морфологическое открытие длинным ГОРИЗОНТАЛЬНЫМ ядром: из
    всех штрихов страницы только у нотной линейки есть непрерывный горизонтальный
    пробег в десятки пикселей. Но у этого приёма есть встроенное допущение —
    линейка прямая и горизонтальная. Наклон θ уводит её на L·sin θ по длине ядра
    L, и уже при 8° это больше толщины штриха: ядро разрубает линейку на куски.

    Отсюда — **разбор по вертикальным полосам**. В каждой полосе кривая линейка
    почти прямая, так что в полосе достаточно оценить локальный угол, выпрямить
    сдвигом (он обратим одной формулой) и открыть ядром уже честно. Полосы идут
    внахлёст, а сшивает их обратно `merge_fragments`.

    Это и есть тот случай, когда изгиб страницы обрабатывается не «вопреки», а
    штатно: каждая полоса живёт со своим углом.
    """
    mask = ink_mask(gray) if mask is None else mask
    height, width = mask.shape[:2]
    max_thickness = max(2.0, max_thickness_ratio * height)
    kernel_len = max(9, width // 45)

    band_width = max(kernel_len * 6, width // max(bands, 1))
    overlap = kernel_len * 2
    fragments: list[StaffLine] = []

    start_x = 0
    while start_x < width:
        stop_x = min(width, start_x + band_width + overlap)
        if stop_x - start_x < kernel_len * 2:
            break
        band = mask[:, start_x:stop_x]
        angle = estimate_skew(band, max_skew_deg)
        for line in _lines_in_band(
            _shear_rows(band, angle), kernel_len, max_thickness
        ):
            # Возврат из выпрямленной системы полосы в исходную: сначала снимаем
            # сдвиг, потом добавляем смещение полосы по X.
            ys = line.ys + np.tan(np.radians(angle)) * line.xs
            fragments.append(StaffLine(line.xs + start_x, ys, line.thickness))
        start_x += band_width

    return sorted(fragments, key=lambda line: line.y_mid)


def _lines_in_band(
    band: np.ndarray, kernel_len: int, max_thickness: float
) -> list[StaffLine]:
    """Горизонтальные штрихи внутри одной (уже выпрямленной) полосы."""
    horizontal = cv2.morphologyEx(band, cv2.MORPH_OPEN, np.ones((1, kernel_len), np.uint8))
    # Закрытие втрое длиннее сшивает разрывы там, где линейку пересёк штиль.
    horizontal = cv2.morphologyEx(
        horizontal, cv2.MORPH_CLOSE, np.ones((1, kernel_len * 3), np.uint8)
    )

    min_len = max(12.0, kernel_len * 2.0)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(horizontal, connectivity=8)
    result: list[StaffLine] = []
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        if w < min_len or h > max_thickness * 3:
            continue
        component = labels[y : y + h, x : x + w] == label
        rows, cols = np.nonzero(component)
        if cols.size == 0:
            continue
        order = np.argsort(cols, kind="stable")
        cols, rows = cols[order], rows[order]
        boundaries = np.searchsorted(cols, np.arange(w + 1))
        counts = np.diff(boundaries)
        filled = counts > 0
        if filled.sum() < min_len:
            continue
        sums = np.add.reduceat(rows, np.clip(boundaries[:-1], 0, len(rows) - 1))
        thickness = float(area / filled.sum())
        if thickness > max_thickness:
            continue
        result.append(StaffLine(
            xs=(np.arange(w)[filled] + x).astype(np.float32),
            ys=(sums[filled] / counts[filled] + y).astype(np.float32),
            thickness=thickness,
        ))
    return result


# ----------------------------------------------------------------------------------
# Сборка линеек в станы
# ----------------------------------------------------------------------------------

@dataclass(eq=False)
class Staff:
    """Пять линеек одного нотоносца, сверху вниз."""

    lines: list[StaffLine]

    @property
    def interline(self) -> float:
        return float(np.median(np.diff([l.y_mid for l in self.lines])))

    @property
    def top(self) -> StaffLine:
        return self.lines[0]

    @property
    def bottom(self) -> StaffLine:
        return self.lines[-1]

    @property
    def x0(self) -> float:
        return float(np.median([l.x0 for l in self.lines]))

    @property
    def x1(self) -> float:
        return float(np.median([l.x1 for l in self.lines]))


def merge_fragments(lines: list[StaffLine], tolerance: float) -> list[StaffLine]:
    """Склеить обрывки одной физической линейки в одну ломаную.

    Тонкая линейка на фотографии рвётся там, где по ней прошлась тень или где её
    перекрыл штиль. Морфологическое закрытие сшивает разрывы в несколько пикселей,
    но не в сотню, — поэтому досклеиваем на уровне объектов: два фрагмента
    считаем одним, если их X-диапазоны почти не пересекаются, а Y в точке стыка
    сходится в пределах `tolerance`.
    """
    if not lines:
        return []
    remaining = sorted(lines, key=lambda l: l.x0)
    merged: list[StaffLine] = []
    while remaining:
        current = remaining.pop(0)
        changed = True
        while changed:
            changed = False
            for other in list(remaining):
                if _joinable(current, other, tolerance):
                    current = _join(current, other)
                    remaining.remove(other)
                    changed = True
        merged.append(current)
    return sorted(merged, key=lambda l: l.y_mid)


def _joinable(a: StaffLine, b: StaffLine, tolerance: float) -> bool:
    """Один ли это физический штрих.

    Решает всегда Y, а не величина перекрытия. Полосы разбора идут внахлёст, так
    что фрагменты ОДНОЙ линейки часто перекрываются почти целиком; если считать
    сильное перекрытие признаком «двух разных линеек», такие дубликаты остаются
    в списке, забивают гистограмму зазоров и рушат оценку межлинейного
    расстояния. А две действительно разные линейки стана отличает как раз Y —
    они разнесены на целый интервал.
    """
    left, right = (a, b) if a.x0 <= b.x0 else (b, a)
    overlap = min(a.x1, b.x1) - max(a.x0, b.x0)
    if overlap > 0:
        x = (max(a.x0, b.x0) + min(a.x1, b.x1)) / 2
        return abs(a.y_at(x) - b.y_at(x)) <= tolerance
    gap = right.x0 - left.x1
    if gap > max(left.length, right.length):
        return False                      # слишком далеко, чтобы быть одной линейкой
    # Экстраполируем левый фрагмент до начала правого и сверяем Y.
    slope = (left.ys[-1] - left.ys[0]) / max(left.length, 1.0)
    predicted = left.ys[-1] + slope * gap
    return abs(predicted - right.ys[0]) <= tolerance


def _join(a: StaffLine, b: StaffLine) -> StaffLine:
    xs = np.concatenate([a.xs, b.xs])
    ys = np.concatenate([a.ys, b.ys])
    order = np.argsort(xs)
    xs, ys = xs[order], ys[order]
    unique, index = np.unique(xs, return_index=True)
    thickness = (a.thickness * a.length + b.thickness * b.length) / max(a.length + b.length, 1.0)
    return StaffLine(unique.astype(np.float32), ys[index].astype(np.float32), thickness)


def extend_lines(
    lines: list[StaffLine], mask: np.ndarray, max_drift: float
) -> list[StaffLine]:
    """Дотянуть линейки до места, где реально кончаются чернила.

    Морфология находит уверенное «тело» линейки, но её хвост теряет: у корешка
    книги кривизна круче всего, и ядро там линейку уже не держит. Потеря
    односторонняя и потому опасная — по этим концам строится правый край
    страницы, и вместе с хвостами наружу уезжает последний такт каждой системы.

    Дотягиваем по одному пикселю: предсказываем Y по наклону последнего участка
    и смотрим, есть ли в этой точке чернила. Пока есть — идём дальше, после
    нескольких промахов подряд останавливаемся. Пошаговая трассировка следует за
    кривизной сама, без допущений о её форме.

    `max_drift` (порядка межлинейного расстояния) не даёт трассировке сползти на
    соседнюю линейку: уйти дальше него от прямого продолжения она не может.
    """
    height, width = mask.shape[:2]
    ink = mask > 0
    result: list[StaffLine] = []
    for line in lines:
        xs, ys = line.xs.tolist(), line.ys.tolist()
        tolerance = max(2.0, line.thickness + 1.5)
        for direction in (1, -1):
            xs, ys = _trace(xs, ys, ink, width, height, direction,
                            tolerance, max_drift)
        order = np.argsort(xs)
        result.append(StaffLine(
            np.asarray(xs, dtype=np.float32)[order],
            np.asarray(ys, dtype=np.float32)[order],
            line.thickness,
        ))
    return result


def _trace(
    xs: list[float], ys: list[float], ink: np.ndarray, width: int, height: int,
    direction: int, tolerance: float, max_drift: float,
) -> tuple[list[float], list[float]]:
    """Шагать в одну сторону, пока под предсказанной точкой есть чернила."""
    if len(xs) < 3:
        return xs, ys

    window = max(8, int(width * 0.02))
    # Разрыв в тактовую черту переступаем, больший считаем концом линейки.
    max_miss = max(6, int(width * 0.01))

    if direction > 0:
        recent = list(zip(xs[-window:], ys[-window:]))
    else:
        recent = list(zip(xs[:window], ys[:window]))
    slope = _slope(recent)
    tail_x, tail_y = recent[-1] if direction > 0 else recent[0]
    start_x, start_y, start_slope = tail_x, tail_y, slope

    added: list[tuple[float, float]] = []
    misses = 0
    x = tail_x + direction
    while 0 <= x < width and misses <= max_miss:
        predicted = tail_y + slope * (x - tail_x)
        straight = start_y + start_slope * (x - start_x)
        if abs(predicted - straight) > max_drift:
            break                                   # трассировка сползает — стоп
        low = int(max(0, round(predicted - tolerance)))
        high = int(min(height, round(predicted + tolerance) + 1))
        column = ink[low:high, int(x)]
        if column.any():
            found = low + float(np.mean(np.nonzero(column)[0]))
            added.append((float(x), found))
            tail_x, tail_y = float(x), found
            recent = (recent + [(tail_x, tail_y)])[-window:]
            slope = _slope(recent)
            misses = 0
        else:
            misses += 1
        x += direction

    return xs + [p[0] for p in added], ys + [p[1] for p in added]


def _slope(points: list[tuple[float, float]]) -> float:
    """dy/dx по концам участка. Знак корректен для обоих направлений обхода."""
    if len(points) < 2:
        return 0.0
    (x0, y0), (x1, y1) = points[0], points[-1]
    return (y1 - y0) / (x1 - x0) if abs(x1 - x0) > 1e-6 else 0.0


def reference_x(lines: list[StaffLine]) -> float:
    """Общий X, в котором сравниваются все линейки.

    Сравнивать по «медианному Y всей ломаной» нельзя: линейки покрывают разные
    участки страницы (и после дотягивания хвостов — тем более), а на изогнутом
    листе Y зависит от того, где именно мерить. Один общий X снимает вопрос.
    """
    if not lines:
        return 0.0
    return float(np.median([(line.x0 + line.x1) / 2 for line in lines]))


def interline_candidates(lines: list[StaffLine], limit: int = 3) -> list[float]:
    """Кандидаты на межлинейное расстояние, от самого населённого пика к менее.

    Основа — гистограмма зазоров между соседями по Y: внутристановых зазоров
    вчетверо больше, чем межстановых, поэтому мода надёжнее медианы. Но мода
    ошибается: на снимке, где линейки местами разорваны, самым населённым
    оказывается пик ПОЛОВИННОГО зазора, и тогда `find_staves` со своим окном
    [0.65×, 1.45×] не собирает ни одного стана — при том, что линеек найдено
    шесть десятков.

    Так и вышло на боевом файле: 3.5 px вместо 6.9, ноль станов вместо шести, и
    хватало разницы в ОДИН пиксель по высоте уменьшенной копии, чтобы результат
    перевернулся. Поэтому кандидатов несколько, а выбор между ними делает тот,
    кто умеет их проверить, — сборка станов (см. `analyse`).
    """
    if len(lines) < 2:
        return []
    x = reference_x(lines)
    ys = np.sort([line.y_at(x) for line in lines])
    gaps = np.diff(ys)
    gaps = gaps[(gaps > 1.5) & (gaps < 200)]
    if gaps.size == 0:
        return []
    histogram, edges = np.histogram(gaps, bins=40)
    candidates: list[float] = []
    for peak in np.argsort(histogram)[::-1]:
        if histogram[peak] == 0:
            break
        inside = gaps[(gaps >= edges[peak]) & (gaps <= edges[peak + 1])]
        if inside.size:
            candidates.append(float(np.median(inside)))
        if len(candidates) >= limit:
            break
    return candidates or [float(np.median(gaps))]


def estimate_interline(lines: list[StaffLine]) -> float | None:
    """Самый населённый зазор между соседними линейками, px.

    Грубая оценка «на один взгляд». Там, где от неё зависит результат, берут
    `interline_candidates` и проверяют их сборкой станов.
    """
    candidates = interline_candidates(lines, limit=1)
    return candidates[0] if candidates else None


def find_staves(lines: list[StaffLine], interline: float) -> list[Staff]:
    """Собрать станы: жадно ищем цепочки ровно из пяти равноудалённых линеек.

    Это и есть главный фильтр ложных срабатываний. Подчёркивание в тексте, балка
    и длинная лига проходят детектор отрезков, но ни у одной из них нет четырёх
    соседей на кратных расстояниях — в стан они не собираются и отсеиваются.
    """
    if not lines or not interline or interline <= 0:
        return []

    x_ref = reference_x(lines)
    lines = sorted(lines, key=lambda line: line.y_at(x_ref))
    staves: list[Staff] = []
    used: set[int] = set()
    lo, hi = interline * 0.65, interline * 1.45

    for start in range(len(lines)):
        if start in used:
            continue
        chain = [start]
        while len(chain) < 5:
            last = lines[chain[-1]]
            candidates = []
            for i in range(chain[-1] + 1, len(lines)):
                if i in used or not _overlaps_horizontally(lines[i], last):
                    continue
                # Замеряем зазор в середине общего участка, а не по медиане Y:
                # на изогнутой странице только так соседи остаются соседями.
                x = (max(lines[i].x0, last.x0) + min(lines[i].x1, last.x1)) / 2
                gap = lines[i].y_at(x) - last.y_at(x)
                if lo <= gap <= hi:
                    candidates.append((gap, i))
            if not candidates:
                break
            chain.append(min(candidates)[1])  # ближайший по зазору
        if len(chain) == 5:
            used.update(chain)
            staves.append(Staff([lines[i] for i in chain]))
    return staves


def _overlaps_horizontally(a: StaffLine, b: StaffLine, min_ratio: float = 0.5) -> bool:
    """Линейки одного стана обязаны идти бок о бок, а не по разным половинам листа."""
    overlap = min(a.x1, b.x1) - max(a.x0, b.x0)
    return overlap >= min_ratio * min(a.length, b.length)


def median_angle(lines: list[StaffLine]) -> float:
    """Медианный наклон линеек, градусы — то, на что нужно довернуть страницу.

    Медиана взвешена длиной: короткий обрывок не должен спорить с полной линейкой.
    """
    if not lines:
        return 0.0
    angles = np.array([l.angle_deg for l in lines])
    weights = np.array([l.length for l in lines])
    order = np.argsort(angles)
    angles, weights = angles[order], weights[order]
    cumulative = np.cumsum(weights)
    return float(angles[int(np.searchsorted(cumulative, cumulative[-1] / 2))])


def analyse(
    gray: np.ndarray,
    *,
    min_len_ratio: float = 0.08,
    max_thickness_ratio: float = 0.006,
    max_skew_deg: float = 12.0,
) -> tuple[list[StaffLine], list[Staff], float | None]:
    """Полный разбор: линейки -> склейка обрывков -> станы -> межлинейный интервал.

    Единая точка входа для всех стадий, чтобы они не повторяли одну и ту же
    последовательность и одинаково понимали, что такое «линейка стана».
    """
    mask = ink_mask(gray)
    fragments = detect_lines(
        gray,
        min_len_ratio=min_len_ratio,
        max_thickness_ratio=max_thickness_ratio,
        max_skew_deg=max_skew_deg,
        mask=mask,
    )
    # Допуск склейки берём от ТОЛЩИНЫ штриха, а не от межлинейного расстояния.
    # Интервал по несклеенным фрагментам считать нельзя: каждая линейка разбита на
    # полосы, и «зазоры» между её же кусками забивают гистограмму — оценка выходит
    # втрое заниженной, склейка с таким допуском не срабатывает, и дальше сыплется
    # всё. Толщина же меряется прямо и от разбиения на полосы не зависит.
    thickness = float(np.median([f.thickness for f in fragments])) if fragments else 2.0
    lines = merge_fragments(fragments, tolerance=max(2.5, 2.5 * thickness))
    # Фильтр по длине — ТОЛЬКО здесь, после склейки. Если отсеивать раньше, на
    # изогнутой странице отвалятся как раз те куски линейки, из которых она и
    # должна была собраться, и правый край блока нот уедет внутрь.
    minimum = max(20.0, min_len_ratio * gray.shape[1])
    lines = [line for line in lines if line.length >= minimum]
    # Порядок важен: сначала грубая оценка интервала — она задаёт, насколько
    # далеко трассировке позволено уйти от прямого продолжения, — и только потом
    # дотягивание хвостов и окончательная оценка.
    rough = estimate_interline(lines) or 10.0
    lines = extend_lines(lines, mask, max_drift=1.5 * rough)
    # Оценку интервала не берём на веру: пробуем несколько кандидатов и оставляем
    # тот, на котором станы вообще собираются. Проверка дешёвая — `find_staves`
    # работает по списку линеек, без обращения к картинке.
    staves: list[Staff] = []
    interline: float | None = None
    for candidate in interline_candidates(lines):
        grouped = find_staves(lines, candidate)
        if len(grouped) > len(staves):
            staves, interline = grouped, candidate
    if interline is None:
        interline = estimate_interline(lines)

    staves = drop_ledger_staves(staves)
    if staves:
        interline = float(np.median([s.interline for s in staves]))
    return lines, staves, interline


def _extent(stave: Staff) -> float:
    return max(l.x1 for l in stave.lines) - min(l.x0 for l in stave.lines)


def drop_ledger_staves(staves: list[Staff]) -> list[Staff]:
    """Убрать «станы», собранные из добавочных линеек соседнего стана.

    Найдено на Бартоке (Out of Doors, стр. 1) и Шопене (op.38, стр. 2): под
    станом идут низкие ноты на трёх-четырёх добавочных линейках подряд, склейка
    сшивает их в длинные линии, и пять таких с шагом интервала собираются в
    «стан» — ровно на интервал ниже настоящего и короче его (63% ширины).
    Детектор насчитывал 11 станов вместо 10, и повтор движка («homr потерял
    стан») срабатывал впустую.

    Второй вид того же (Шопен op.38, стр. 2): «стан» во всю ширину, но в
    промежутке между станами фортепиано, собранный из добавочных линеек и
    октавной линии с шагом 15.3 px при 10.5-11 у всех настоящих станов страницы.

    Настоящие станы так близко не стоят — даже внутри гранд-стана между ними
    несколько интервалов, — и шаг линеек у них на странице почти одинаковый.
    Поэтому стан ближе двух интервалов к соседу считаем добавочными линейками,
    если он заметно короче соседа или его шаг отличается от обычного для
    страницы больше чем на четверть (а у соседа — нет).
    """
    if len(staves) < 2:
        return staves
    usual = float(np.median([s.interline for s in staves]))

    def odd_size(stave: Staff) -> bool:
        return abs(stave.interline / usual - 1.0) > 0.25

    kept = []
    for stave in staves:
        ledger = False
        for other in staves:
            if other is stave:
                continue
            gap = (max(stave.top.y_mid, other.top.y_mid)
                   - min(stave.bottom.y_mid, other.bottom.y_mid))
            if gap >= 2 * max(stave.interline, other.interline):
                continue
            shorter = _extent(stave) < 0.8 * _extent(other)
            if shorter or (odd_size(stave) and not odd_size(other)):
                ledger = True
                break
        if not ledger:
            kept.append(stave)
    return kept
