#!/usr/bin/env python3
"""Запуск homr отдельным процессом + защита от падения на вырожденном стане.

Отдельный процесс — потому что onnxruntime поднимает сотни мегабайт и держать
его в веб-воркере незачем; запускается и умирает вместе с задачей.

Защита. В `homr/staff_parsing.py` размеры кропа стана считаются целочисленным
умножением на масштабный коэффициент, и на очень узком или низком стане
результат округляется в НОЛЬ. Дальше OpenCV падает — то в `cv2.resize`
(`inv_scale_x > 0`), то в `cv2.threshold` на пустой матрице (та возвращает None,
и следующая строка ловит `TypeError`), — и вместе с ним падает весь процесс homr.
Итог: один кривой стан из двадцати убивает распознавание ВСЕЙ страницы.

Защищаемся на двух уровнях:

1. `cv2.resize` внутри модуля не может получить нулевой размер — почти
   вырожденный стан переживает обработку и остаётся в результате;
2. разбор ОДНОГО стана обёрнут в try/except. Что бы там ни сломалось, стан
   отдаётся пустым, а `parse_staffs` такие пропускает штатно
   («Skipping empty staff»). Страница теряет один стан вместо всего выхода.

Второй уровень тут главный: он ловит и те падения, которых мы ещё не видели.

Скрипт намеренно не импортирует ничего из `omr` — он должен запускаться тем
интерпретатором, где стоит homr, каким бы тот ни был.
"""

import sys
import threading
from pathlib import Path

import numpy as np


class _SafeCv2:
    """Прокси над cv2, у которого resize не умеет получить нулевой размер."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def resize(self, src, dsize, *args, **kwargs):
        if dsize is not None:
            width, height = dsize
            dsize = (max(int(width), 1), max(int(height), 1))
        return self._real.resize(src, dsize, *args, **kwargs)


def _install_canvas_guard() -> None:
    import cv2

    from homr import staff_parsing

    # Главная защита: любой resize внутри staff_parsing получает размер >= 1.
    staff_parsing.cv2 = _SafeCv2(cv2)

    def clamp(shape):
        return np.maximum(np.asarray(shape), 1)

    # Плюс точечные патчи на исходные места — они дешёвые и оставляют размеры
    # согласованными между собой, а не только неотрицательными. Каждый под
    # hasattr: если в новой версии homr функцию переименуют, мы просто
    # запустимся без этой части защиты, а не упадём на импорте раннера.
    if hasattr(staff_parsing, "get_tr_omr_canvas_size"):
        original = staff_parsing.get_tr_omr_canvas_size
        staff_parsing.get_tr_omr_canvas_size = lambda *a, **k: clamp(original(*a, **k))

    if hasattr(staff_parsing, "center_image_on_canvas"):
        original_center = staff_parsing.center_image_on_canvas
        staff_parsing.center_image_on_canvas = (
            lambda image, canvas, *a, **k: original_center(image, clamp(canvas), *a, **k)
        )

    # Ловушка последней надежды: сбой на одном стане не должен ронять страницу.
    if hasattr(staff_parsing, "parse_staff_image"):
        original_parse = staff_parsing.parse_staff_image

        def safe_parse(debug, index, *args, **kwargs):
            try:
                return original_parse(debug, index, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — ради этого всё и затевалось
                print(f"staff {index} skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
                return []

        staff_parsing.parse_staff_image = safe_parse


def _install_tempo_capture(image_path: str) -> None:
    """Сохранить СЫРЫЕ строки OCR, которые homr читает над верхним станом.

    `title_detection` кропает полосу над верхним станом, гоняет по ней OCR и ищет
    там заголовок. Метрономная отметка попадает в этот же кроп, но до нас не
    доезжает ни одним путём: строку без четырёх букв («♩=95») он отбрасывает как
    не-заголовок, а если отметка слиплась с текстом в одну строку OCR
    («♩ = 117 "Water"»), то её съедает `cleanup_text` — он оставляет только буквы
    и цифры, и «=» исчезает вместе с самим признаком темпа.

    Поэтому перехватываем ВСЕ строки до фильтра и до чистки. `is_tempo_marking`
    для этого удобна тем, что через неё проходит каждая строка ровно один раз;
    вердикт её мы не трогаем. OCR уже отработал — темп достаётся даром, а иначе
    за него приходится отдельно гонять Audiveris на весь лист.

    Пишем в файл рядом со входом (как homr пишет свой MusicXML) — построчно и
    сразу, потому что `detect_title` работает в отдельном потоке и процесс может
    закончиться раньше, чем мы соберём всё в память.
    """
    from homr import title_detection

    if not hasattr(title_detection, "is_tempo_marking"):
        return  # переименовали — просто останемся без бесплатного темпа

    original = title_detection.is_tempo_marking
    sidecar = Path(image_path).with_suffix(".ocr.txt")
    lock = threading.Lock()

    def capturing(text):
        if text and text.strip():
            try:
                with lock, sidecar.open("a", encoding="utf-8") as handle:
                    handle.write(" ".join(text.split()) + "\n")
            except OSError:
                pass  # не смогли записать — не повод ронять распознавание
        return original(text)

    title_detection.is_tempo_marking = capturing


if __name__ == "__main__":
    try:
        _install_canvas_guard()
    except Exception as exc:  # noqa: BLE001 — защита необязательна, запуск важнее
        print(f"canvas guard not installed: {exc}", file=sys.stderr)
    try:
        _install_tempo_capture(sys.argv[1])
    except Exception as exc:  # noqa: BLE001 — то же: перехват темпа не обязателен
        print(f"tempo capture not installed: {exc}", file=sys.stderr)
    from homr.main import main

    main()
