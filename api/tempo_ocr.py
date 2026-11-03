"""Темп со страницы: OCR над первым станом → метрономная отметка.

Для homr темп читаем мы, а не движок: он OCR-ит полосу над верхним станом своим
RapidOCR, раннер перехватывает строки, а `api.analysis.bpm_from_texts` находит в
них «♩ = 95». Ударные идут в Audiveris, и темп там до сих пор был его: на корпусе
`tests/images/drum/pdmx` он совпадал с эталоном на 14 PDF из 20 и на 21 снимке из 40,
и почти всегда промах — отметка не найдена вовсе (плеер тогда играет 120). Здесь тот
же определитель без homr: тот же RapidOCR (в образе он стоит как зависимость homr),
то же место над станом, тот же разбор. С ним — 20 из 20 и 37 из 40 (три снимка
Audiveris не распознал совсем, темп там ни при чём).

Где искать. Вся ширина страницы от верхнего края, а не 15 интервалов над станом, как
у homr: MuseScore ставит отметку в левый верхний угол над названием, и на Wonderwall
она в 18 интервалах над станом и левее его начала. Название и авторы туда тоже
попадают — числа рядом с «=» в них не бывает, разбор их пропускает.

Где граница. OCR получает страницу до низа первого стана, а строку мы берём, только
если её центр выше верхней линии стана в той же точке x. Резать картинку по одной
высоте нельзя дважды: на фото стан наклонён, и срез по верхнему краю линии отрезал
низ отметки у другого её конца (Pull Me Under); а балки шестнадцатых над станом
`omr.staff.analyse` склеивает в «линейки», и верх стана находится на интервал-два
выше настоящего (Samba) — отметка, стоящая вплотную к нотам, резалась пополам.

Строки идут слева направо: отметок над первым станом бывает несколько (Venus: 90 в
первом такте и 95 во втором), а темп начала — левая. Нота отметки бывает половинной
(`half_note`), тогда четвертей в минуту вдвое больше напечатанного числа; точку при
ноте не различаем.

Сам OCR — отдельным процессом на задачу (`tempo_ocr_runner.py`), как homr: вызов
onnxruntime на полосе страницы поднимал веб-процесс API на 0.7-1 ГБ, и память не
возвращалась. Модель грузится один раз на задачу, все картинки идут одним запуском;
поиск стана и разбор остаются здесь. Шаг целиком — 1.8 c на файл (медиана корпуса,
максимум 3.1 c) при медиане Audiveris 18.6 c; в процессе было 1.2 c, но память дороже.
"""

from __future__ import annotations

import json
import math
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Однолинейный стан `omr.staff.analyse` станом не считает; берём верхнюю длинную
# линию. Доля ширины страницы — с запасом от названия, подчёркнутого чертой.
ONE_LINE_MIN_WIDTH = 0.3

# Сколько страниц смотреть в поисках первого стана: обложка бывает одна.
MAX_PAGES = 3

# PDF рисуем сами, под OCR, а не под Audiveris. Лист A4 на 300 DPI — 8.7 Мп; «фото,
# сохранённое в PDF» бывает размером с плакат, его ужимаем до потолка.
PDF_DPI = 300
PDF_MAX_PIXELS = 12_000_000

# Интервал стана, к которому приводим страницу перед OCR, — по картинке на каждое
# значение; второй масштаб в дело идёт, если в первом отметки нет. Детектор строк
# RapidOCR чувствителен к размеру текста: на PDF корпуса без приведения теряются «=» у
# Pull Me Under и Reggaeton (читается голое «102»), при 24 px — King Cadence, при
# 28 px — Reggaeton; два масштаба находят все три. Ложной отметки на листах без темпа
# не дал ни один масштаб.
OCR_INTERLINES = (24.0, 28.0)

RUNNER = Path(__file__).with_name("tempo_ocr_runner.py")
# Запуск — около 2 c: импорт onnxruntime, модель, две картинки.
OCR_TIMEOUT = 60


class OcrError(RuntimeError):
    """Раннер OCR не отработал: нет RapidOCR в интерпретаторе, таймаут, падение."""


@dataclass
class TempoReading:
    """Что нашлось на страницах. `page` — номер страницы со станом (с 0), None — стана нет."""

    printed: int | None = None     # число с отметки, как напечатано
    unit: str = "quarter"          # нота отметки: quarter или half
    page: int | None = None
    texts: list[str] = field(default_factory=list)

    @property
    def bpm(self) -> int | None:
        """Четвертей в минуту — в этих единицах темп пишется в `<sound tempo>`."""
        if self.printed is None:
            return None
        return self.printed * 2 if self.unit == "half" else self.printed

    def describe(self) -> str:
        if self.page is None:
            return "темп по OCR: ни на одной странице не нашлось стана"
        shown = ", ".join(repr(text) for text in self.texts[:8]) or "текста нет"
        if self.printed is None:
            found = "отметки нет"
        elif self.unit == "half":
            found = f"половинная={self.printed} (♩={self.bpm})"
        else:
            found = f"♩={self.printed}"
        return f"темп по OCR (стр. {self.page + 1}): {found}; строки над станом: {shown}"


def _ocr_batch(images: list[np.ndarray]) -> list[list[tuple[np.ndarray, str]]]:
    """OCR картинок одним запуском раннера; на каждую — [(четыре угла строки, текст)].

    Интерпретатор — тот, где стоит homr (`OMR_HOMR_PYTHON` или текущий): RapidOCR
    приходит вместе с ним.
    """
    import cv2

    from omr.engines.homr_engine import _run, interpreter

    with tempfile.TemporaryDirectory(prefix="tempo-ocr-") as tmp:
        paths = []
        for index, image in enumerate(images):
            path = Path(tmp) / f"{index}.png"
            cv2.imwrite(str(path), image)
            paths.append(str(path))
        target = Path(tmp) / "ocr.json"
        done = _run([interpreter(), str(RUNNER), str(target), *paths], OCR_TIMEOUT)
        if done.returncode != 0 or not target.exists():
            reason = (done.stderr or "").strip().splitlines()[-1:] or ["без вывода"]
            raise OcrError(f"OCR темпа не отработал (код {done.returncode}): {reason[0][:200]}")
        pages = json.loads(target.read_text(encoding="utf-8"))
    return [[(np.asarray(box, np.float32), text) for box, text in lines] for lines in pages]


def first_staff(gray: np.ndarray):
    """(верхняя линия первого стана, низ первого стана в px, интервал); None — стана нет.

    Интервал None у однолинейного стана: `analyse` меряет его по соседним линиям, а
    их нет.
    """
    from omr.staff import analyse

    lines, staves, _ = analyse(gray)
    if staves:
        staff = min(staves, key=lambda candidate: candidate.top.y_mid)
        return staff.top, float(np.max(staff.bottom.ys)), staff.interline
    long_lines = [line for line in lines if line.length >= ONE_LINE_MIN_WIDTH * gray.shape[1]]
    if not long_lines:
        return None
    line = min(long_lines, key=lambda candidate: candidate.y_mid)
    return line, float(np.max(line.ys)) + 2 * line.thickness, None


def _lines_above(lines: list[tuple[np.ndarray, str]], top, scale: float) -> list[tuple[np.ndarray, str]]:
    """Строки, чей центр выше линии `top`, слева направо; рамки — в координатах страницы."""
    above = []
    for box, text in lines:
        box = box / scale
        x, y = box.mean(axis=0)
        if y < top.y_at(x):
            above.append((box, text))
    return sorted(above, key=lambda line: float(line[0][:, 0].min()))


def half_note(gray: np.ndarray, box: np.ndarray) -> bool:
    """Нота отметки с полой головкой — половинная («𝅗𝅥 = 70», Little Drummer Boy)?

    OCR ноту читает буквой «J» независимо от головки, а темп от неё вдвое другой.
    Нота — левый высокий знак строки (рамка OCR её часто не включает, поэтому
    захватываем левее): со штилем она выше цифр. Знаки берём только с серединой внутри
    рамки по высоте — ниже начинается стан, и кусок ключа слева от ноты на фото LDB
    сходил за неё. Полая — если в нижней половине знака есть дырка заметной площади.
    Нет уверенности — четверть, как было.
    """
    import cv2

    xs, ys = box[:, 0], box[:, 1]
    height = float(ys.max() - ys.min())
    left, right = int(max(0, xs.min() - 0.5 * height)), int(min(gray.shape[1], xs.max()))
    top, bottom = int(max(0, ys.min() - 0.3 * height)), int(min(gray.shape[0], ys.max() + 0.3 * height))
    crop = gray[top:bottom, left:right]
    if crop.size == 0:
        return False
    _, ink = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    box_top, box_bottom = ys.min() - top, ys.max() - top
    tall = [i for i in range(1, count)
            if stats[i, cv2.CC_STAT_HEIGHT] >= 0.4 * height
            and stats[i, cv2.CC_STAT_WIDTH] >= 0.15 * height
            and box_top <= stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT] / 2 <= box_bottom]
    if len(tall) < 2:
        return False
    note = min(tall, key=lambda i: stats[i, cv2.CC_STAT_LEFT])
    digits = [stats[i, cv2.CC_STAT_HEIGHT] for i in tall if i != note]
    if stats[note, cv2.CC_STAT_HEIGHT] < 1.15 * float(np.median(digits)):
        return False   # левый знак — цифра: ноты в кадре нет
    width = float(stats[note, cv2.CC_STAT_WIDTH])
    middle = stats[note, cv2.CC_STAT_TOP] + stats[note, cv2.CC_STAT_HEIGHT] / 2
    contours, hierarchy = cv2.findContours((labels == note).astype(np.uint8), cv2.RETR_CCOMP,
                                           cv2.CHAIN_APPROX_SIMPLE)
    for contour, (_, _, _, parent) in zip(contours, hierarchy[0] if hierarchy is not None else []):
        if parent < 0 or cv2.contourArea(contour) < 0.08 * width * width:
            continue
        if float(contour[:, 0, 1].mean()) > middle:
            return True
    return False


def read_page(gray: np.ndarray) -> TempoReading | None:
    """Отметка над первым станом страницы; None — стана на странице нет."""
    import cv2

    from api.analysis import bpm_from_texts

    staff = first_staff(gray)
    if staff is None:
        return None
    top, bottom, interline = staff
    region = gray[: int(min(gray.shape[0], bottom + 1))]
    scales = [min(max(target / interline, 0.5), 3.0) for target in OCR_INTERLINES] if interline else [1.0]
    images = [region if scale == 1 else
              cv2.resize(region, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
              for scale in scales]
    shown: list[str] | None = None
    for scale, found in zip(scales, _ocr_batch(images)):
        lines = _lines_above(found, top, scale)
        texts = [text for _, text in lines]
        for box, text in lines:
            printed = bpm_from_texts([text])
            if printed is not None:
                unit = "half" if half_note(gray, box) else "quarter"
                return TempoReading(printed=printed, unit=unit, texts=texts)
        if shown is None:
            shown = texts
    return TempoReading(texts=shown or [])


def _pages(path: Path, pdf_dpi: int) -> Iterator[np.ndarray]:
    import cv2

    if path.suffix.lower() != ".pdf":
        gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if gray is not None:
            yield gray
        return
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        for index in range(min(len(document), MAX_PAGES)):
            page = document[index]
            width, height = page.get_size()
            scale = min(pdf_dpi / 72.0, math.sqrt(PDF_MAX_PIXELS / max(width * height, 1.0)))
            yield np.asarray(page.render(scale=scale).to_pil().convert("L"))
    finally:
        document.close()


def read_tempo(paths: list[Path], pdf_dpi: int = PDF_DPI) -> TempoReading:
    """Темп начала: отметка над первым станом первой страницы, где стан есть.

    Страницы без стана (обложка, колофон) пропускаем. Дальше первой страницы с нотами
    не идём: отметка на следующей — это смена темпа посреди пьесы, а не темп начала
    (Venus: 90 на первой странице, 105 на второй).
    """
    index = 0
    for path in paths:
        for gray in _pages(path, pdf_dpi):
            if index >= MAX_PAGES:
                return TempoReading()
            reading = read_page(gray)
            if reading is not None:
                reading.page = index
                return reading
            index += 1
    return TempoReading()
