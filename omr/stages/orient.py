"""Стадия 0.5 — определить ориентацию страницы (портрет/альбом).

Появилась не из общих соображений, а по разбору реальных провалов прода: из
шести файлов, на которых распознавание падало, ТРИ оказались страницами,
повёрнутыми на 90°. Скан положили в сканер боком — и всё, нотные линейки
вертикальные, движок не находит ни одного стана.

Ни один из остальных механизмов такое не ловит: оценка перекоса ищет угол в
пределах ±12°, а поиск страницы по линейкам на повёрнутом листе просто не
находит линеек. Поэтому проверка стоит первой, до всей геометрии.

Критерий прямой: разворачиваем копию на 90° и смотрим, где нотных линеек
собирается в станы больше. Никаких эвристик про соотношение сторон — альбомная
партитура существует, а вот стан, лежащий на боку, не существует.

Сторону поворота (по часовой или против) выбираем по положению КЛЮЧА: он стоит в
начале каждого стана, поэтому у правильно повёрнутой страницы левый край стана
заметно «чернее» правого. Без этой проверки половина сайдвейс-снимков приезжает
вверх ногами — линейки-то горизонтальные в обоих случаях.

Чего эта стадия НЕ умеет: развернуть страницу, которая пришла вверх ногами и БЕЗ
поворота на 90°. Признак тот же самый (ключи слева), но там мы бы переворачивали
изначально нормальный кадр по одной эвристике, а цена ошибки выше — оставлено
на потом.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from omr import staff
from omr.config import PipelineConfig


@dataclass
class OrientationInfo:
    rotated: bool
    staves_upright: int
    staves_rotated: int
    clockwise: bool = True
    clef_score: float = 0.0

    @property
    def reason(self) -> str:
        if self.rotated:
            side = "по часовой" if self.clockwise else "против часовой"
            return (f"повёрнута на 90° {side} "
                    f"(станов {self.staves_rotated} против {self.staves_upright}, "
                    f"ключи {self.clef_score:+.2f})")
        return "портретная"


def fix_orientation(
    image: np.ndarray, gray: np.ndarray, staves_found: int, config: PipelineConfig
) -> tuple[np.ndarray, OrientationInfo]:
    """Повернуть страницу на 90°, если так нотных станов находится больше.

    `gray` и `staves_found` — уже посчитанный разбор исходной ориентации, чтобы
    не гонять детектор дважды по одному и тому же.
    """
    # Если станов и так набралось достаточно, страница точно не лежит на боку:
    # у повёрнутого листа их находится ноль. Экономим лишний разбор.
    if staves_found >= config.analysis_enough_staves:
        return image, OrientationInfo(False, staves_found, 0)

    rotated_gray = cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
    _, rotated_staves, _ = staff.analyse(
        rotated_gray,
        min_len_ratio=config.min_line_len_ratio,
        max_thickness_ratio=config.max_line_thickness_ratio,
        max_skew_deg=config.max_skew_deg,
    )
    if len(rotated_staves) <= staves_found + config.orientation_margin:
        return image, OrientationInfo(False, staves_found, len(rotated_staves))

    # Куда именно поворачивать. Обе стороны дают горизонтальные линейки, и по ним
    # их не различить: они отличаются ровно на 180°. Зато поворот на 180°
    # МЕНЯЕТ ЛЕВЫЙ КРАЙ СТАНА С ПРАВЫМ, а ключ стоит в начале стана — значит
    # оценка «насколько левый край чернее правого» для второй стороны просто
    # меняет знак, и второй разбор не нужен.
    score = _clef_side_score(rotated_gray, rotated_staves)
    clockwise = score >= 0
    rotation = cv2.ROTATE_90_CLOCKWISE if clockwise else cv2.ROTATE_90_COUNTERCLOCKWISE
    return cv2.rotate(image, rotation), OrientationInfo(
        True, staves_found, len(rotated_staves), clockwise, score
    )


def _clef_side_score(gray: np.ndarray, staves: list) -> float:
    """Насколько чернила стана смещены к ЛЕВОМУ краю. >0 — страница стоит верно.

    Меряем не количество чернил, а ДОЛЮ СТРОК полосы, в которых чернила вообще
    есть. Ключ со знаками при ключе закрывает почти всю высоту стана подряд, и
    таких строк в начале стана заметно больше, чем в конце, где стоит одна
    тактовая черта. Средняя плотность для этого не годится: на рукописной или
    плотно исписанной странице правый конец бывает чернее левого, и знак
    переворачивается (проверено на `rot90-03-soranji` — по плотности он уезжал
    вверх ногами, по покрытию строк встаёт верно).

    Признак СЛАБЫЙ, и это надо держать в голове. На пяти сайдвейс-снимках из
    архива провалов он угадывает 5 из 5 (слепое «всегда по часовой» — 3 из 5), но
    запас у половины из них в районе 0.01-0.07: решение фактически принимается на
    грани. Поэтому оценка выводится в отчёт стадии — когда страница приедет вверх
    ногами, по ней сразу видно, что виновата именно эта развилка.
    """
    mask = staff.ink_mask(gray)
    height = mask.shape[0]
    scores: list[float] = []
    for item in staves:
        interline = item.interline or 10.0
        y0 = int(max(0, item.top.y_mid - 3 * interline))
        y1 = int(min(height, item.bottom.y_mid + 3 * interline))
        x0, x1 = int(item.x0), int(item.x1)
        band = max(8, int(0.12 * (x1 - x0)))
        if y1 - y0 < 4 or x1 - x0 < 4 * band:
            continue
        left = mask[y0:y1, x0 : x0 + band]
        right = mask[y0:y1, x1 - band : x1]
        if left.size and right.size:
            scores.append(
                float((left > 0).any(axis=1).mean() - (right > 0).any(axis=1).mean())
            )
    return float(np.median(scores)) if scores else 0.0
