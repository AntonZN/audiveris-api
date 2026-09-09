"""Стадия 2 — гомография: перспективный варп четырёхугольника в прямоугольник."""

from __future__ import annotations

import cv2
import numpy as np

from omr import geometry


def rectify(image: np.ndarray, quad: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Развернуть `quad` в прямоугольник. Возвращает (картинка, матрица 3x3).

    Фон за пределами исходного кадра заливаем БЕЛЫМ, а не чёрным: рамка страницы
    может немного выходить за края снимка, и белое поле движок воспримет как
    обычные поля листа, тогда как чёрная полоса выглядит для сегментации как
    огромный штрих и притягивает ложные линейки.
    """
    width, height = geometry.quad_size(quad)
    target = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32
    )
    matrix = cv2.getPerspectiveTransform(np.asarray(quad, dtype=np.float32), target)
    border = (255, 255, 255) if image.ndim == 3 else 255
    warped = cv2.warpPerspective(
        image, matrix, (width, height),
        flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=border,
    )
    return warped, matrix
