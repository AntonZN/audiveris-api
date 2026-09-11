"""Ударные: маршрут в Audiveris и интервал для однолинейного стана.

homr ударных не знает: перкуссионный ключ читает как альтовый, крестовые головки
теряет — на `tests/images/drum` 0-4%. Audiveris с `drumNotation` и шрифтом Leland
на тех же эталонах даёт 96-100%. Поэтому пресеты ударных идут прямо в Audiveris,
минуя omr и homr (см. `AudiverisService._recognize_drums`).

Однолинейный стан. Интервал Audiveris меряет по расстоянию между линиями, а у
одной линии его нет — без подсказки он падает с «No regularly spaced lines
found». Подсказку (`defaultInterlineSpecification`) оцениваем по закрашенным
головкам: пик distance transform в центре головки — половина её высоты, а это и
есть интервал. У штилей, балок и самой линии пики вытянуты в хребет и
отсекаются; точки, буквы и крестики мельче головок и проигрывают при весе по
площади. Замер: на четырёх PDF ударных при 300 DPI — ровно 20, столько же
намерил сам Audiveris; на фото `fail_prod_files/perc-01` — 18 при рабочем окне
Audiveris 18-20.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np

from api.presets import Preset

DRUM_PRESETS = frozenset({Preset.drums.value, Preset.drums_1line.value})
INTERLINE_CONSTANT = "org.audiveris.omr.sheet.Scale.defaultInterlineSpecification"

# Растр Audiveris читает через ImageIO, PDF — через PDFBox. WebP и HEIC — нет,
# их надо переложить заранее.
AUDIVERIS_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".pdf"})

# Порядок попыток вокруг оценки. Окно Audiveris узкое: на perc-01 работают 18-20,
# а 16 и 17 роняют его NPE, 14 и 22 рвут лист на movements.
LADDER = (0, 1, -1, 2)

# Столько пятен одного размера нужно, чтобы считать их головками: одинокая
# клякса (тень на фото, жирная буква заголовка) не должна задавать интервал.
MIN_HEADS = 5


def is_drum_preset(preset: str | None) -> bool:
    return preset in DRUM_PRESETS


def needs_interline(preset: str | None) -> bool:
    return preset == Preset.drums_1line.value


def ladder(estimate: int, floor: int) -> list[int]:
    """Интервалы для попыток: оценка, затем соседи; не ниже порога Audiveris."""
    return [value for value in dict.fromkeys(estimate + step for step in LADDER)
            if value >= floor]


def page_gray(path: Path, pdf_dpi: int) -> np.ndarray | None:
    """Первая страница в оттенках серого — так, как её увидит Audiveris.

    PDF рисуем на том же DPI, что и Audiveris: интервал в пикселях зависит от него.
    """
    import cv2

    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(path))
        try:
            if len(document) == 0:
                return None
            image = document[0].render(scale=pdf_dpi / 72.0).to_pil().convert("L")
        finally:
            document.close()
        return np.asarray(image)
    return cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)


def estimate_interline(gray: np.ndarray | None) -> int | None:
    """Интервал стана в пикселях по высоте закрашенных головок; None — не нашли."""
    import cv2

    if gray is None or gray.size == 0:
        return None
    # Otsu, а не адаптивный порог: адаптивный с окном меньше пары головок
    # выедает их середину, и головка превращается в кольцо (на скане scan-01
    # оценка уезжала с 25 до 6).
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    distance = cv2.distanceTransform(ink, cv2.DIST_L2, 5)
    peak = (distance >= cv2.dilate(distance, np.ones((3, 3), np.uint8))) & (distance >= 2)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        peak.astype(np.uint8), connectivity=8
    )
    if count <= 1:
        return None
    radius = np.zeros(count, np.float32)
    inside = labels > 0
    np.maximum.at(radius, labels[inside], distance[inside])
    # Компактный пик — центр пятна; хребет штриха длиннее своей толщины.
    extent = np.maximum(stats[:, 2], stats[:, 3])
    compact = (np.arange(count) > 0) & (extent <= radius)
    sizes = Counter(np.rint(2 * radius[compact]).astype(int).tolist())
    weights = {size: n * size * size for size, n in sizes.items() if n >= MIN_HEADS}
    if not weights:
        return None
    size = max(weights, key=weights.get)
    return size if 4 <= size <= min(gray.shape) // 8 else None
