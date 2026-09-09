"""Стадия 5 — привести яркость и масштаб к тому, что ждёт движок.

Две вещи, каждая со своим «почему»:

**Освещение.** Снимок телефона всегда неравномерно освещён: один угол
пересвечен, другой в тени от руки. Делим картинку на оценку фона — остаётся
ровная белая бумага, штрихи не трогаются.

**Масштаб.** homr внутри ужимает вход до фиксированной ширины 1920 px и уже на
ней ищет линейки. Значит, отдавать ему картинку шире бессмысленно (он всё равно
выбросит детали), а уже — вредно (он растянет и размоет). Отдаём ровно 1920: его
собственный resize становится no-op.

Отдельно оговорка: **бинаризовать здесь нельзя**. Сегментация homr — свёрточная
сеть, обученная на естественных изображениях; чёрно-белая маска для неё вход не
из распределения. Порог мы используем только внутри, чтобы мерить геометрию.
"""

from __future__ import annotations

import cv2
import numpy as np

from omr.config import PipelineConfig


def flatten_illumination(image: np.ndarray, config: PipelineConfig) -> np.ndarray:
    """Убрать градиент освещения делением на морфологическую оценку фона.

    Фон = морфологическое закрытие большим ядром: оно «заливает» все штрихи и
    оставляет только медленно меняющуюся яркость бумаги. Ядро берём заведомо
    больше самого толстого штриха, иначе оценка фона съест ноты.
    """
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    size = max(15, int(min(gray.shape[:2]) * config.illumination_kernel_ratio) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    background = cv2.GaussianBlur(background, (0, 0), size / 3.0)
    flat = gray.astype(np.float32) / np.maximum(background.astype(np.float32), 1.0)
    return np.clip(flat * 235.0, 0, 255).astype(np.uint8)


def scale_for_engine(
    image: np.ndarray, interline: float | None, config: PipelineConfig
) -> tuple[np.ndarray, float, str]:
    """Отмасштабировать под движок. Возвращает (картинка, коэффициент, замечание).

    Целевая ширина — единственный ориентир, и он не обсуждается: homr всё равно
    приведёт вход к своим 1920 px, так что любой другой размер он просто
    переинтерполирует ещё раз. Отдаём ровно 1920 — его resize становится no-op.

    Межлинейное расстояние на масштаб поэтому НЕ влияет (влиять ему бессмысленно),
    но проверяется и попадает в отчёт: если после приведения к 1920 px интервал
    выходит за рабочий коридор, распознавание будет плохим по причине, которую
    никакая подготовка не лечит, — на снимке просто мало пикселей на нотный стан
    (или, наоборот, в кадр попал один-единственный стан крупным планом).
    """
    height, width = image.shape[:2]
    scale = min(config.target_width / width, config.max_upscale)

    note = ""
    if interline:
        projected = interline * scale
        if projected < config.min_interline_px:
            note = f"стан слишком мелкий ({projected:.1f}px на интервал)"
        elif projected > config.max_interline_px:
            note = f"стан слишком крупный ({projected:.1f}px на интервал)"

    if abs(scale - 1.0) < 0.02:
        return image, 1.0, note
    # INTER_AREA при уменьшении (честное усреднение, без алиасинга на линейках),
    # INTER_CUBIC при увеличении.
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    resized = cv2.resize(
        image, (max(int(width * scale), 1), max(int(height * scale), 1)),
        interpolation=interpolation,
    )
    return resized, float(scale), note
