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

Снимки (фото, скриншоты). PDF Audiveris рисует сам на 300 DPI и читает на 95-100%,
а снимок ему надо готовить: нужен интервал около 24 px и чёрно-белая картинка.
Скриншот телефона шириной 1170 px — это интервал 10, ниже порога Audiveris; на
фото мешают неровный свет и серый фон. Поэтому снимок масштабируем до интервала
`TARGET_INTERLINE`, выравниваем свет делением на фон и бинаризуем Otsu
(`photo_plan`, `prepare_photo`). Подготовка `omr/` для homr тут не годится: она
сжимает страницу до ширины 1920, и на фото Audiveris читает хуже, чем сырьё.

Однолинейный лист, который и так не мельче `ONE_LINE_NATIVE`, не растягиваем и не
бинаризуем — только свет. Толщину линии Audiveris меряет по гистограмме высот
чёрных отрезков, и у пятилинейного стана пик толщины держат пять линий, а у
однолинейного — одна. Растяжение размытого фото размазывает этот пик, и Audiveris
отказывает на шаге SCALE («No significant black lines found»): `perc-01` с
интервалом 18 при любом растяжении падал, без него — 18 тактов из 32. На
синтетических фото однолинейных листов корпуса (интервал 17) — ничья: 97/61/96 с
растяжением против 75/94/91 без него, так что решает реальный снимок.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import numpy as np

from api.presets import Preset, get_preset_args

DRUM_PRESETS = frozenset({Preset.drums.value, Preset.drums_1line.value})
INTERLINE_CONSTANT = "org.audiveris.omr.sheet.Scale.defaultInterlineSpecification"
_ONE_LINE_STAVES = "org.audiveris.omr.sheet.ProcessingSwitches.oneLineStaves"
# Повтор `-constant` перекрывает значение из пресета: Audiveris складывает их в
# Properties по порядку (CLI.PropertyOptionHandler).
NO_ONE_LINE_STAVES = f"{_ONE_LINE_STAVES}=false"

# Снимки, которые готовим сами (`prepare_photo`); Audiveris получает PNG. HEIC
# читается через pillow-heif (omr/stages/load.py).
PHOTO_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic", ".heif",
})

# Интервал, до которого масштабируем снимок. Скриншоты установок корпуса
# tests/images/drum/pdmx: 80-100% против отказа у сырого снимка; фото: 78-100%
# против 33-92%.
TARGET_INTERLINE = 24.0

# С этого интервала однолинейный лист читается без растяжения (фото — 17-18 px);
# скриншоту с интервалом 10 растяжение с Otsu нужно: 96-100% против 0-55%.
ONE_LINE_NATIVE = 16

# Audiveris не берёт лист больше 20 Мп (LoadStep, «Too large image»); с запасом,
# как и бюджет рендера PDF (`pdf_max_pixels`).
MAX_PIXELS = 18_000_000

# Окно фона в интервалах: заметно больше такта, чтобы закрытие не залило балки и
# группы головок и не приняло их за фон.
BACKGROUND_WINDOW = 6
BACKGROUND_SHRINK = 4

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


def reads_one_line_staves(preset: str | None) -> bool:
    """Однолинейные станы — дополнение к пятилинейным: без них лист ещё читается.

    У `drums_1line` ключ — единственный источник станов, повторять без него нечего.
    """
    if not is_drum_preset(preset) or needs_interline(preset):
        return False
    return f"{_ONE_LINE_STAVES}=true" in get_preset_args(Preset(preset))


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


def photo_interline(gray: np.ndarray, one_line: bool) -> float | None:
    """Интервал стана на снимке; None — стана не нашли.

    Пятилинейный — по линиям (`omr.staff.analyse`). Однолинейный — по головкам:
    линий для разметки там нет, и analyse собирает «стан» из случайных длинных
    штрихов (на Drummer's Delight интервал 137 px вместо 10).
    """
    if one_line:
        value = estimate_interline(gray)
        return float(value) if value is not None else None
    from omr.staff import analyse

    _, staves, interline = analyse(gray)
    return interline if staves else None


def photo_plan(shape: tuple[int, ...], interline: float, one_line: bool) -> tuple[float, bool]:
    """(масштаб, бинаризовать ли) для снимка; больше `MAX_PIXELS` не растягиваем никогда.

    Обычно — до `TARGET_INTERLINE` и Otsu. Однолинейный лист не мельче
    `ONE_LINE_NATIVE` остаётся в своём масштабе и сером цвете (см. описание модуля).
    """
    height, width = shape[:2]
    fitting = math.sqrt(MAX_PIXELS / (height * width))
    if one_line and interline >= ONE_LINE_NATIVE:
        return min(1.0, fitting), False
    return min(TARGET_INTERLINE / interline, fitting), True


def prepare_photo(gray: np.ndarray, scale: float, interline: float,
                  binarize: bool = True) -> np.ndarray:
    """Снимок для Audiveris: масштаб, свет делением на фон и, если `binarize`, Otsu.

    `interline` — интервал уже после масштаба, по нему окно фона. Фон — закрытие
    (чернила стираются, остаются бумага и тени) и размытие, чтобы ступени закрытия
    не легли на ноты. Считаем его на копии в `BACKGROUND_SHRINK` раз меньше: фон
    плавный, а закрытие окном в 150 px на 14 Мп идёт 4.6 c против 0.1 c; бинарная
    картинка расходится в 0.05% пикселей, по краям штрихов.
    """
    import cv2

    image = gray
    if scale != 1:
        interpolation = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
        image = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=interpolation)
    small = cv2.resize(image, None, fx=1 / BACKGROUND_SHRINK, fy=1 / BACKGROUND_SHRINK,
                       interpolation=cv2.INTER_AREA)
    window = int(interline * BACKGROUND_WINDOW / BACKGROUND_SHRINK) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (window, window))
    background = cv2.GaussianBlur(cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel), (window, window), 0)
    background = cv2.resize(background, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_LINEAR)
    flat = np.clip(image.astype(np.float32) / np.maximum(background, 1) * 255, 0, 255).astype(np.uint8)
    if not binarize:
        return flat
    _, binary = cv2.threshold(flat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary
