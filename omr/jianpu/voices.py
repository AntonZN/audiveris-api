"""Многоголосие в цзянпу (хор, дуэт, две строки фортепиано) — отказ до движка.

    python -m omr.jianpu.voices page.png другая.jpg

Зачем. Движок (jpeditor) многоголосия не знает: строки голосов одной системы он
читает подряд как одну мелодию. На хоре SATB (`tests/images/jianpu/document (2).pdf`)
выходила одна партия с размером 6/2, и API отдавал это клиенту как успех.
Честная ошибка лучше.

Признак — вертикаль через несколько строк текста. В одноголосном цзянпу
вертикали короткие: тактовая черта — в высоту строки цифр, вольта и реприза — не
выше. Голоса одной системы объединяет скобка слева (так у хора из набора), у части
издателей ещё и тактовые черты проходят через все голоса.

Чем признак НЕ является — просто длинной линией. На фото страницы сборника край
листа и тень корешка дают десятки вертикалей во всю высоту кадра; рамка вокруг
приложенного текста песни — вертикаль со строками справа. Поэтому:

* скобка голосов — не выше 70% листа, справа от неё строки текста, и почти весь
  текст этих строк правее неё; парная вертикаль той же высоты правее — это рамка;
* сквозная черта — через несколько строк, и текст по обе стороны; одна такая
  может быть случайной, нужны три.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from omr.stages import load as load_stage

# Рабочая ширина: признак считается в высотах текста, лишние пиксели его не уточняют.
WORK_WIDTH = 2000


@dataclass
class Vertical:
    x: float
    top: float
    bottom: float

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass
class VoicesReport:
    text_height: float = 0.0
    brackets: list[int] = field(default_factory=list)   # строк текста у каждой скобки голосов
    through: list[int] = field(default_factory=list)    # строк у каждой сквозной черты

    @property
    def multi(self) -> bool:
        return (len(self.brackets) >= 2 or any(rows >= 3 for rows in self.brackets)
                or len(self.through) >= 3)

    def summary(self) -> str:
        return (f"скобок голосов {len(self.brackets)}, сквозных черт {len(self.through)}, "
                f"высота текста {self.text_height:.0f}px")


def detect(image: np.ndarray | Path) -> VoicesReport:
    picture = load_stage.load_bgr(Path(image)) if isinstance(image, (str, Path)) else image
    gray = cv2.cvtColor(picture, cv2.COLOR_BGR2GRAY) if picture.ndim == 3 else picture
    if gray.shape[1] > WORK_WIDTH:
        factor = WORK_WIDTH / gray.shape[1]
        gray = cv2.resize(gray, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    # Адаптивный порог: на фото тень у края листа глобальный порог режет пятнами.
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    page_height = gray.shape[0]

    _, _, stats, centroids = cv2.connectedComponentsWithStats(ink, connectivity=8)
    heights, widths = stats[1:, cv2.CC_STAT_HEIGHT], stats[1:, cv2.CC_STAT_WIDTH]
    textlike = ((heights > page_height * 0.006) & (heights < page_height * 0.05)
                & (widths > heights * 0.15) & (widths < heights * 1.3))
    report = VoicesReport()
    if textlike.sum() < 20:
        return report
    text = float(np.median(heights[textlike]))
    report.text_height = text
    glyphs = centroids[1:][textlike]

    page_width = gray.shape[1]
    # Край кадра на фото книги — тёмная полоса во всю высоту или её куски: на
    # `image0.png` (сборник для эрху) все длинные вертикали стоят на x <= 20 из 2000.
    lines = [line for line in _verticals(ink, text)
             if 3 * text <= line.height <= 0.7 * page_height
             and 0.015 * page_width < line.x < 0.985 * page_width]
    for line in lines:
        inside = glyphs[(glyphs[:, 1] >= line.top - 0.3 * text) & (glyphs[:, 1] <= line.bottom + 0.3 * text)]
        right = inside[inside[:, 0] > line.x]
        left = inside[inside[:, 0] < line.x]
        rows_right = _rows(right[right[:, 0] < line.x + 15 * text], text)
        if rows_right >= 2 and len(right) >= 5 * max(len(left), 1) and not _framed(line, lines, text):
            report.brackets.append(rows_right)
            continue
        rows_left = _rows(left[left[:, 0] > line.x - 15 * text], text)
        if min(rows_right, rows_left) >= 2 and min(len(left), len(right)) >= 0.25 * len(inside):
            report.through.append(min(rows_right, rows_left))
    return report


def _verticals(ink: np.ndarray, text: float) -> list[Vertical]:
    """Почти вертикальные отрезки длиннее 2.5 высот текста, склеенные по разрывам.

    Ищутся не по всей краске, а только среди вытянутых по вертикали компонент.
    Иначе столбец иероглифов из семи плотных куплетов Хаф собирает в «черту»
    через пять строк — так ложно срабатывал синтетический `jiangsu2_1026`.
    """
    _, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    tall, wide = stats[:, cv2.CC_STAT_HEIGHT], stats[:, cv2.CC_STAT_WIDTH]
    keep = (tall >= 1.5 * text) & (wide <= 0.35 * tall)
    keep[0] = False
    mask = np.where(keep[labels], 255, 0).astype(np.uint8)
    segments = cv2.HoughLinesP(mask, 1, np.pi / 360, threshold=int(2 * text),
                               minLineLength=int(2.5 * text), maxLineGap=max(2, int(0.3 * text)))
    if segments is None:
        return []
    found = []
    for x1, y1, x2, y2 in segments[:, 0].astype(int):
        rise = abs(y2 - y1)
        # Допуск на наклон — фото держатся в пределах пары градусов.
        if rise and abs(x2 - x1) <= 0.08 * rise:
            found.append(Vertical((x1 + x2) / 2, min(y1, y2), max(y1, y2)))
    # Сначала столбцы по x, потом внутри столбца интервалы сверху вниз. Склейка в
    # порядке «как пришли» от порядка и зависела: отрезок из середины скобки
    # заводил свою линию, соседние дорастали до неё и уже не сливались — одна
    # скобка считалась за две.
    columns: list[list[Vertical]] = []
    for segment in sorted(found, key=lambda v: v.x):
        if columns and segment.x - columns[-1][-1].x <= 0.6 * text:
            columns[-1].append(segment)
        else:
            columns.append([segment])
    merged: list[Vertical] = []
    for column in columns:
        current: Vertical | None = None
        for segment in sorted(column, key=lambda v: v.top):
            # Разрыв склеиваем только маленький: черты соседних строк, стоящие
            # друг под другом, иначе сложились бы в одну «сквозную».
            if current is not None and segment.top <= current.bottom + 0.3 * text:
                current.bottom = max(current.bottom, segment.bottom)
            else:
                current = Vertical(segment.x, segment.top, segment.bottom)
                merged.append(current)
    return merged


def _rows(glyphs: np.ndarray, text: float) -> int:
    """Сколько строк текста среди знаков: разрыв по вертикали больше 0.9 высоты — новая строка."""
    if len(glyphs) == 0:
        return 0
    ys = np.sort(glyphs[:, 1])
    breaks = np.flatnonzero(np.diff(ys) > 0.9 * text)
    sizes = np.diff(np.concatenate(([0], breaks + 1, [len(ys)])))
    return int((sizes >= 2).sum())


def _framed(line: Vertical, lines: list[Vertical], text: float) -> bool:
    """Есть ли правее вертикаль той же высоты — тогда это рамка, а не скобка голосов."""
    return any(other.x > line.x + 10 * text and abs(other.top - line.top) <= text
               and abs(other.bottom - line.bottom) <= text for other in lines)


def main(argv: list[str] | None = None) -> int:
    for item in argv if argv is not None else sys.argv[1:]:
        report = detect(Path(item))
        print(f"{Path(item).name:32s} {'МНОГОГОЛОСИЕ' if report.multi else 'один голос  '}  "
              f"{report.summary()}  скобки {report.brackets} черты {report.through}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
