#!/usr/bin/env python3
"""Раннер homr с монкипатчем от краша на вырожденном стане.

Баг homr 0.6.2 (`homr/staff_parsing.py`): `get_tr_omr_canvas_size` считает целевой
размер канвы для TrOMR как `int(width/height * max_height)`. Для очень УЗКОГО и
ВЫСОКОГО кропа стана это округляется в **0**, и `center_image_on_canvas` вызывает
`cv2.resize(image, (0, H))` → OpenCV падает с `(-215) inv_scale_x > 0` и роняет ВЕСЬ
процесс homr. Итог: один кривой стан (напр. 18-й из 21) убивает распознавание всей
страницы → «нет MusicXML на выходе». Подтверждено: max_height=256, max_width=1280,
для h≫w new_shape[0]=0.

Фикс: клампим размеры канвы до ≥1 пикселя. Такой стан распознаётся мусором, но homr
НЕ падает и доводит остальные станы до MusicXML — страница спасена.

Запускается как отдельный скрипт (`python homr_patch.py <image> [homr-флаги...]`),
НЕ импортирует пакет `api`, поэтому работает независимо от CWD. Аргументы прозрачно
уходят в homr.main.main() (argparse читает sys.argv[1:]).
"""

import numpy as np
from homr import staff_parsing


def _clamp_shape(shape: object) -> np.ndarray:
    """Гарантировать, что все размеры канвы ≥1 (иначе cv2.resize падает)."""
    return np.maximum(np.asarray(shape), 1)


# Патчим на уровне модуля: внутренние вызовы в staff_parsing резолвят имена как
# глобальные модуля в момент вызова, поэтому подмена атрибутов действует.
# Каждый патч под hasattr-охраной: если апгрейд homr переименует функцию, мы просто
# запустим homr БЕЗ патча (вернётся исходный баг на вырожденном стане), но не
# уроним homr целиком из-за AttributeError на импорте раннера.
if hasattr(staff_parsing, "get_tr_omr_canvas_size"):
    _orig_canvas_size = staff_parsing.get_tr_omr_canvas_size

    def _safe_canvas_size(*args: object, **kwargs: object) -> np.ndarray:
        return _clamp_shape(_orig_canvas_size(*args, **kwargs))

    staff_parsing.get_tr_omr_canvas_size = _safe_canvas_size

# Дублирующая защита прямо перед cv2.resize — на случай другого источника нулевого
# размера (напр. изменённый вызов в будущих версиях).
if hasattr(staff_parsing, "center_image_on_canvas"):
    _orig_center = staff_parsing.center_image_on_canvas

    def _safe_center(
        image: object, canvas_size: object, *args: object, **kwargs: object
    ) -> object:
        return _orig_center(image, _clamp_shape(canvas_size), *args, **kwargs)

    staff_parsing.center_image_on_canvas = _safe_center


if __name__ == "__main__":
    from homr.main import main

    main()
