#!/usr/bin/env python3
"""OCR картинок для api/tempo_ocr.py — отдельным процессом, как homr.

    tempo_ocr_runner.py <выход.json> <картинка> [<картинка> ...]

Отдельный процесс — по той же причине, что у homr (omr/engines/_homr_runner.py):
onnxruntime на полосе страницы поднимает процесс на 0.7-1 ГБ, и аллокатор их потом
не отдаёт, а веб-процессу API держать это незачем. Модель грузится один раз на
запуск, запуск — один на задачу: все картинки приходят разом.

В выход пишет JSON: на каждую картинку список [четыре угла строки, текст]. Не в
stdout — туда пишет журнал сам RapidOCR.

Скрипт намеренно не импортирует ничего из `api` и `omr`: он запускается тем
интерпретатором, где стоит homr (а с ним и RapidOCR), каким бы тот ни был.
"""

import json
import sys


def _reader():
    """RapidOCR, который стоит рядом с homr: 0.6.2 тянет `rapidocr_onnxruntime`, 0.7 — `rapidocr`."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        from rapidocr import RapidOCR

        engine = RapidOCR()

        def read(path):
            output = engine(path)
            if output.txts is None:
                return []
            return list(zip(output.boxes, output.txts))

        return read

    engine = RapidOCR()

    def read(path):
        result, _ = engine(path)
        return [(box, text) for box, text, _ in result or []]

    return read


def main(argv: list[str]) -> int:
    target, images = argv[0], argv[1:]
    read = _reader()
    pages = [[[[[float(value) for value in point] for point in box], text]
              for box, text in read(path)]
             for path in images]
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(pages, handle, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
