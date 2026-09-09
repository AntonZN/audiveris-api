"""Отладочные картинки: смотреть глазами быстрее, чем читать числа.

Каждая стадия пишет сюда свой кадр, и в конце по папке `--debug` видно, где
именно пайплайн ошибся: не нашёл линейки, взял не тот четырёхугольник, перегнул
с ремапом.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


class DebugWriter:
    """Пишет пронумерованные кадры в папку. Отключённый — не делает ничего."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = Path(directory) if directory else None
        self._counter = 0
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.directory is not None

    def image(self, name: str, image: np.ndarray) -> None:
        if not self.directory:
            return
        self._counter += 1
        path = self.directory / f"{self._counter:02d}_{name}.png"
        cv2.imwrite(str(path), image)

    def lines(self, name: str, image: np.ndarray, lines, staves=()) -> None:
        """Линейки жёлтым, собранные станы — цветными рамками."""
        if not self.directory:
            return
        canvas = _to_bgr(image)
        for line in lines:
            points = np.stack([line.xs, line.ys], axis=1).astype(np.int32)
            cv2.polylines(canvas, [points], False, (0, 220, 255), 1)
        palette = [(255, 80, 80), (80, 255, 80), (80, 80, 255), (255, 80, 255), (255, 200, 0)]
        for index, staff in enumerate(staves):
            color = palette[index % len(palette)]
            x0, x1 = int(staff.x0), int(staff.x1)
            y0, y1 = int(staff.top.y_mid), int(staff.bottom.y_mid)
            cv2.rectangle(canvas, (x0, y0 - 3), (x1, y1 + 3), color, 2)
            cv2.putText(canvas, str(index), (x0 + 4, y0 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        self.image(name, canvas)

    def quad(self, name: str, image: np.ndarray, quad: np.ndarray, label: str = "") -> None:
        if not self.directory:
            return
        canvas = _to_bgr(image)
        cv2.polylines(canvas, [np.asarray(quad, np.int32)], True, (0, 0, 255), 3)
        for index, point in enumerate(np.asarray(quad, np.int32)):
            cv2.circle(canvas, tuple(point), 8, (255, 0, 0), -1)
            cv2.putText(canvas, "tl tr br bl".split()[index], tuple(point + 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)
        if label:
            cv2.putText(canvas, label, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        self.image(name, canvas)


def _to_bgr(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    return image.copy()
