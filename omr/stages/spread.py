"""Разворот книги: две страницы в одном кадре — разрезать по корешку.

Зачем. На развороте станов вдвое больше, чем на странице, и все они делят одну и
ту же целевую ширину движка: после приведения к 1920 px на межлинейное
расстояние остаётся 6-7 px вместо нормальных 15-20. Движку этого не хватает.
Разрезав кадр по корешку, мы отдаём ему две страницы, каждая из которых получает
свои 1920 px, — вдвое больше пикселей на те же ноты.

Есть и вторая причина, менее очевидная. Разворот — это НЕ одна плоскость: две
половины лежат под разными углами. Одна гомография на обе половины принципиально
не может выпрямить обе. После разреза каждая половина получает свою рамку, свою
гомографию и свой dewarp.

Как ищем корешок. По профилю покрытия колонок нотными линейками: на развороте
линейки обрываются у корешка, и в середине кадра образуется провал. Мерить надо
**по сырым фрагментам, ДО их склейки**: `merge_fragments` спокойно перемахивает
через корешок (на плоском скане станы обеих страниц стоят на одной высоте, и
фрагменты слева и справа выглядят как одна разорванная линейка) и затирает весь
сигнал. Разница принципиальная — замеры на реальных файлах:

    файл                     по фрагментам   после склейки
    spread-01 (разворот)         0.02            0.82
    book-02   (разворот)         0.02            0.51
    scan-01   (одна стр.)        0.93            0.95
    phone-01  (одна стр.)        0.90            0.95

По фрагментам развороты и одиночные страницы разделяются без перекрытия.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from omr import staff
from omr.config import PipelineConfig
from omr.stages import load


@dataclass
class SpreadInfo:
    is_spread: bool
    reason: str
    split_x: int | None = None       # в координатах ИСХОДНОГО изображения
    valley_ratio: float = 1.0
    left_lines: int = 0
    right_lines: int = 0


def detect(image: np.ndarray, config: PipelineConfig) -> SpreadInfo:
    """Найти корешок разворота. Ищем ВЕРТИКАЛЬНЫЙ провал в покрытии колонок.

    Лежащий на боку разворот сюда не попадёт: у него линейки вертикальные,
    детектор их не найдёт, и мы честно скажем «мало фрагментов». Это безопасный
    отказ — хуже не станет, просто разворот останется неразрезанным.
    """
    gray = load.to_gray(image)
    small, scale = load.downscale_to_width(gray, config.analysis_width)
    fragments = staff.detect_lines(
        small,
        min_len_ratio=config.min_line_len_ratio,
        max_thickness_ratio=config.max_line_thickness_ratio,
        max_skew_deg=config.max_skew_deg,
    )
    if len(fragments) < config.spread_min_fragments:
        return SpreadInfo(False, f"фрагментов мало ({len(fragments)})")

    width = small.shape[1]
    coverage = np.zeros(width, dtype=np.int32)
    for line in fragments:
        coverage[int(line.x0) : int(line.x1) + 1] += 1

    # Блок нот: где покрытие вообще есть. Поля страницы в поиске не участвуют.
    inside = np.where(coverage > coverage.max() * 0.2)[0]
    if inside.size < width * 0.2:
        return SpreadInfo(False, "блок нот слишком узкий")
    left_edge, right_edge = int(inside[0]), int(inside[-1])
    band = coverage[left_edge : right_edge + 1]
    plateau = float(np.median(band)) or 1.0

    # Корешок ищем только в середине: провал у края — это поле, а не корешок.
    low = int(len(band) * 0.25)
    high = int(len(band) * 0.75)
    centre = band[low:high]
    if centre.size < 10:
        return SpreadInfo(False, "середина блока слишком узкая")

    ratio = float(centre.min()) / plateau
    if ratio > config.spread_gutter_ratio:
        return SpreadInfo(False, f"провала в середине нет ({ratio:.2f})", valley_ratio=ratio)

    # Берём СЕРЕДИНУ самого широкого провала, а не первый минимум: корешок это
    # полоса в несколько пикселей, и резать надо по её центру.
    threshold = plateau * config.spread_gutter_ratio
    split_local = _widest_low_run(centre, threshold) + low + left_edge

    left = sum(1 for line in fragments if (line.x0 + line.x1) / 2 < split_local)
    right = len(fragments) - left
    share = config.spread_min_side_share * len(fragments)
    if left < share or right < share:
        return SpreadInfo(
            False, f"по одну сторону почти нет нот ({left}/{right})", valley_ratio=ratio
        )

    return SpreadInfo(
        True,
        f"корешок найден (провал {ratio:.2f})",
        split_x=int(round(split_local / scale)),
        valley_ratio=ratio,
        left_lines=left,
        right_lines=right,
    )


def _widest_low_run(values: np.ndarray, threshold: float) -> int:
    """Центр самого широкого участка, где покрытие ниже порога."""
    low = values <= threshold
    best_start = best_length = 0
    start = None
    for index, is_low in enumerate(low):
        if is_low and start is None:
            start = index
        elif not is_low and start is not None:
            if index - start > best_length:
                best_start, best_length = start, index - start
            start = None
    if start is not None and len(low) - start > best_length:
        best_start, best_length = start, len(low) - start
    if best_length == 0:
        return int(np.argmin(values))
    return best_start + best_length // 2


def split(image: np.ndarray, split_x: int) -> list[np.ndarray]:
    """Разрезать кадр по корешку на левую и правую страницы (в порядке чтения)."""
    width = image.shape[1]
    cut = max(1, min(width - 1, int(split_x)))
    return [image[:, :cut].copy(), image[:, cut:].copy()]
