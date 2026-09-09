"""Стадия 4 — точный доворот по нотным линейкам.

Гомография уже сделала страницу прямоугольной, но остаточный поворот в доли
градуса переживает её легко: он приходит из того, что края блока нот оценены с
точностью до пикселя. Здесь мы измеряем угол не по краям, а по САМИМ линейкам —
их на странице полсотни, и медиана их наклона на порядок точнее.

Именно этот шаг пользователь и описывал: посчитать angle каждой линейки, взять
медиану и довернуть на неё.
"""

from __future__ import annotations

import cv2
import numpy as np

from omr import staff
from omr.config import PipelineConfig


def deskew(
    image: np.ndarray, config: PipelineConfig
) -> tuple[np.ndarray, float, list[float]]:
    """Довернуть картинку так, чтобы линейки стали горизонтальными.

    Итеративно, но не больше `deskew_max_passes` проходов: каждый поворот — это
    интерполяция, а значит небольшое размытие, и гоняться за сотыми долями
    градуса ценой резкости невыгодно.
    """
    applied: list[float] = []
    current = image
    for _ in range(config.deskew_max_passes):
        gray = _gray(current)
        lines = staff.detect_lines(
            gray,
            min_len_ratio=config.min_line_len_ratio,
            max_thickness_ratio=config.max_line_thickness_ratio,
            bands=1,   # тут уже почти ровно — одна полоса на всю ширину
            max_skew_deg=config.max_skew_deg,
        )
        angle = staff.median_angle(lines)
        if abs(angle) < config.deskew_min_deg:
            break
        current = rotate(current, angle)
        applied.append(round(angle, 3))
    return current, float(sum(applied)), applied


def rotate(image: np.ndarray, angle_deg: float) -> np.ndarray:
    """Повернуть вокруг центра, расширив холст, чтобы углы не срезались."""
    height, width = image.shape[:2]
    centre = (width / 2, height / 2)
    matrix = cv2.getRotationMatrix2D(centre, angle_deg, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_width = int(height * sin + width * cos)
    new_height = int(height * cos + width * sin)
    matrix[0, 2] += new_width / 2 - centre[0]
    matrix[1, 2] += new_height / 2 - centre[1]
    border = (255, 255, 255) if image.ndim == 3 else 255
    return cv2.warpAffine(
        image, matrix, (new_width, new_height),
        flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=border,
    )


def _gray(image: np.ndarray) -> np.ndarray:
    return image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
