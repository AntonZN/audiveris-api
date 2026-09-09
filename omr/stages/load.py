"""Стадия 0 — чтение файла.

Отдельным модулем, потому что «просто imread» здесь недостаточно: HEIC с
айфона OpenCV не читает вовсе, а у любого телефонного снимка ориентация лежит в
EXIF, и её надо применить ДО геометрии — иначе мы будем старательно выпрямлять
лежащую на боку страницу.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


class UnreadableImage(Exception):
    """Файл не картинка или формат не поддержан."""


def load_bgr(path: Path) -> np.ndarray:
    """Прочитать снимок как BGR с уже применённой EXIF-ориентацией."""
    path = Path(path)
    if not path.exists():
        # Иначе отсутствие файла выглядит как «формат не поддержан» — cv2 и
        # Pillow на этом ведут себя одинаково молча.
        raise UnreadableImage(f"файла нет: {path}")
    # cv2.imread применяет EXIF-ориентацию сам (если не просить обратного),
    # поэтому основной путь — он: быстрее и без лишних зависимостей.
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is not None:
        return image
    return _load_via_pillow(path)


def _load_via_pillow(path: Path) -> np.ndarray:
    """Запасной путь для HEIC/HEIF и прочей экзотики."""
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover
        raise UnreadableImage(f"не могу прочитать {path.name}: нет Pillow") from exc

    heif_ready = True
    try:
        import pillow_heif  # noqa: F401  — регистрирует HEIF-декодер в Pillow

        pillow_heif.register_heif_opener()
    except ImportError:
        heif_ready = False  # без плагина HEIC не откроется — скажем об этом ниже

    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)      # EXIF-ориентация
            rgb = np.asarray(img.convert("RGB"))
    except Exception as exc:
        # Про pillow-heif пишем только когда его действительно нет: иначе
        # подсказка врёт и отправляет разбираться не туда.
        hint = "" if heif_ready else " Для HEIC/HEIF нужен пакет pillow-heif."
        raise UnreadableImage(f"не могу прочитать {path.name}: {exc}.{hint}") from exc
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def to_gray(image: np.ndarray) -> np.ndarray:
    return image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def downscale_to_width(image: np.ndarray, target_width: int) -> tuple[np.ndarray, float]:
    """Уменьшить до ширины `target_width`. Возвращает (картинка, коэффициент)."""
    width = image.shape[1]
    if width <= target_width:
        return image, 1.0
    scale = target_width / width
    small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return small, scale
