"""Тесты стенда Zeus: группировка станов в системы и нарезка кропов.

Отдельный файл, потому что и код живёт отдельно — в `zeus_eval/`, а не в `omr/`:
единственный его потребитель это движок, читающий по одному стану. homr
гранд-станы группирует сам.
"""

from __future__ import annotations

import unittest

import numpy as np

try:
    import cv2

    from omr import staff
    from omr.stages import load
    from zeus_eval.systems import crop_staves, crop_systems, group_into_systems
except ImportError:  # pragma: no cover
    cv2 = None

if cv2 is not None:
    from tests.test_omr import make_page


@unittest.skipIf(cv2 is None, "нужен opencv-python-headless")
class CropStavesTest(unittest.TestCase):
    """Нарезка страницы на станы — вход для движков, читающих по одному стану."""

    def test_one_crop_per_staff_in_reading_order(self) -> None:
        page = make_page(staves=6)
        _, staves, _ = staff.analyse(load.to_gray(page))
        crops = crop_staves(page, staves)
        self.assertEqual(len(crops), 6)
        tops = [c.box[1] for c in crops]
        self.assertEqual(tops, sorted(tops), "кропы должны идти сверху вниз")

    def test_crop_does_not_swallow_the_neighbouring_staff(self) -> None:
        """Модель, обученная на ОДНОМ стане, на двух в кадре читает мусор."""
        page = make_page(staves=6, interline=12)
        _, staves, _ = staff.analyse(load.to_gray(page))
        crops = crop_staves(page, staves)
        for item in crops:
            _, inside, _ = staff.analyse(load.to_gray(item.image))
            self.assertLessEqual(len(inside), 1, f"в кроп {item.index} заехал сосед")

    def test_crop_keeps_the_whole_staff(self) -> None:
        page = make_page(staves=4, interline=12)
        _, staves, _ = staff.analyse(load.to_gray(page))
        crops = crop_staves(page, staves)
        for item, source in zip(crops, sorted(staves, key=lambda s: s.top.y_mid)):
            x0, y0, x1, y1 = item.box
            self.assertLessEqual(y0, source.top.y_mid)
            self.assertGreaterEqual(y1, source.bottom.y_mid)
            self.assertLessEqual(x0, source.x0)
            self.assertGreaterEqual(x1, source.x1)

    def test_no_staves_means_no_crops(self) -> None:
        blank = np.full((400, 600, 3), 255, np.uint8)
        self.assertEqual(crop_staves(blank, []), [])


@unittest.skipIf(cv2 is None, "нужен opencv-python-headless")
class GrandStaffTest(unittest.TestCase):
    """Гранд-стан опознаётся по СВЯЗНОСТИ промежутка, а не по расстоянию."""

    @staticmethod
    def piano_page(systems: int = 3, bridge: bool = True) -> np.ndarray:
        """Страница из пар нотоносцев, соединённых тактовыми чертами."""
        page = make_page(staves=systems * 2, interline=12)
        gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
        _, staves, _ = staff.analyse(gray)
        staves = sorted(staves, key=lambda s: s.top.y_mid)
        if bridge:
            # Соединяем нотоносцы ПАРАМИ: 0-1, 2-3, ... — как акколада и тактовые
            # черты в фортепианной партитуре.
            for index in range(0, len(staves) - 1, 2):
                upper, lower = staves[index], staves[index + 1]
                y0, y1 = int(upper.bottom.y_mid), int(lower.top.y_mid)
                for x in range(int(upper.x0), int(upper.x1), 120):
                    page[y0:y1, x : x + 3] = 0
        return page

    def test_pairs_staves_that_are_bridged(self) -> None:
        page = self.piano_page(systems=3)
        gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
        _, staves, _ = staff.analyse(gray)
        systems = group_into_systems(staves, staff.ink_mask(gray))
        self.assertEqual([len(s) for s in systems], [2, 2, 2])

    def test_leaves_unbridged_staves_alone(self) -> None:
        """Однолинейная партия: промежутки пусты, пар быть не должно."""
        page = self.piano_page(systems=3, bridge=False)
        gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
        _, staves, _ = staff.analyse(gray)
        systems = group_into_systems(staves, staff.ink_mask(gray))
        self.assertTrue(all(len(s) == 1 for s in systems), [len(s) for s in systems])

    def test_never_groups_more_than_two(self) -> None:
        """Жадное паросочетание не должно склеивать три нотоносца в один кроп."""
        page = make_page(staves=6, interline=12)
        gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
        _, staves, _ = staff.analyse(gray)
        page_all = page.copy()
        ordered = sorted(staves, key=lambda s: s.top.y_mid)
        for upper, lower in zip(ordered, ordered[1:]):      # соединяем ВСЕ подряд
            y0, y1 = int(upper.bottom.y_mid), int(lower.top.y_mid)
            for x in range(int(upper.x0), int(upper.x1), 120):
                page_all[y0:y1, x : x + 3] = 0
        gray_all = cv2.cvtColor(page_all, cv2.COLOR_BGR2GRAY)
        _, staves_all, _ = staff.analyse(gray_all)
        systems = group_into_systems(staves_all, staff.ink_mask(gray_all))
        self.assertTrue(all(len(s) <= 2 for s in systems), [len(s) for s in systems])

    def test_crop_systems_keeps_a_pair_together(self) -> None:
        """Резать гранд-стан пополам нельзя — руки звучат одновременно."""
        page = self.piano_page(systems=3)
        gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
        _, staves, _ = staff.analyse(gray)
        systems = group_into_systems(staves, staff.ink_mask(gray))
        crops = crop_systems(page, systems)
        self.assertEqual(len(crops), 3)
        for item in crops:
            self.assertEqual(item.staves, 2)
            _, inside, _ = staff.analyse(load.to_gray(item.image))
            self.assertEqual(len(inside), 2, "в кроп должны попасть оба нотоносца")


if __name__ == "__main__":
    unittest.main()
