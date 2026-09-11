"""Ударные: пресет уводит задачу в Audiveris, однолинейному стану нужен интервал.

homr ударных не знает (перкуссионный ключ читает как альтовый, крестики теряет),
поэтому `drums`/`drums_1line` идут прямо в Audiveris. Сторожится: маршрут, отказ
от отката на homr, лесенка интервалов для однолинейного стана и сама оценка
интервала по головкам.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from api import drums, omr_bridge, services
from api.exceptions import ProcessingError
from api.models import FileResult

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None


def _snare_line(head: int) -> np.ndarray:
    """Однолинейный стан: головки под линией, штили, балка, крестики хай-хэта."""
    image = np.full((420, 1500), 255, np.uint8)
    y = 210
    cv2.line(image, (20, y), (1480, y), 0, 2)
    for i in range(16):
        x = 60 + i * 85
        cy = y + head // 2 + 2
        cv2.ellipse(image, (x, cy), (int(head * 0.65), head // 2), -20, 0, 360, 0, -1)
        stem = x + int(head * 0.6)
        cv2.line(image, (stem, cy), (stem, y - 80), 0, 2)
        cv2.line(image, (x - 7, y - 45), (x + 7, y - 31), 0, 2)
        cv2.line(image, (x - 7, y - 31), (x + 7, y - 45), 0, 2)
    cv2.line(image, (60 + int(head * 0.6), y - 80), (60 + 15 * 85 + int(head * 0.6), y - 80), 0, 8)
    return image


@unittest.skipIf(cv2 is None, "нужен OpenCV")
class InterlineEstimateTest(unittest.TestCase):
    def test_interline_is_the_notehead_height(self) -> None:
        for head in (18, 26):
            with self.subTest(head=head):
                self.assertAlmostEqual(drums.estimate_interline(_snare_line(head)), head, delta=1)

    def test_blank_page_gives_no_estimate(self) -> None:
        self.assertIsNone(drums.estimate_interline(np.full((400, 600), 255, np.uint8)))
        self.assertIsNone(drums.estimate_interline(None))

    def test_ladder_tries_neighbours_but_not_below_the_floor(self) -> None:
        self.assertEqual(drums.ladder(18, 9), [18, 19, 17, 20])
        self.assertEqual(drums.ladder(9, 9), [9, 10, 11])


class DrumRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.photo = self.out / "page.png"
        # Содержимое неважно: движки замоканы, файл нужен как путь.
        self.photo.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.addCleanup(self._tmp.cleanup)
        self.service = services.AudiverisService()
        self.done = (self.out / "page.mxl", self.out / "page.log", 20)

    def test_drums_go_straight_to_audiveris(self) -> None:
        with mock.patch.object(omr_bridge, "run") as omr, \
             mock.patch.object(services.homr_service, "run") as homr, \
             mock.patch.object(self.service, "_run_audiveris", return_value=self.done) as audiveris, \
             mock.patch.object(self.service, "_build_success_result",
                               return_value=FileResult(filename="page.mxl")):
            result = self.service.process_single(self.photo, self.out, preset="drums")

        self.assertIsNone(result.error)
        audiveris.assert_called_once()
        _, target, preset, extra, _ = audiveris.call_args.args
        self.assertEqual((target, preset, list(extra)), (self.out, "drums", []))
        omr.assert_not_called()
        homr.assert_not_called()

    def test_failed_drums_do_not_fall_back_to_homr(self) -> None:
        """homr на ударных выдал бы «успешный» мусор — лучше честная ошибка."""
        with mock.patch.object(services.homr_service, "run") as homr, \
             mock.patch.object(self.service, "_run_audiveris",
                               side_effect=ProcessingError("Audiveris failed")):
            result = self.service.process_single(self.photo, self.out, preset="drums")

        self.assertEqual(result.error, "Audiveris failed")
        homr.assert_not_called()

    def test_one_line_drums_climb_the_interline_ladder(self) -> None:
        """Окно Audiveris узкое: при провале на оценке пробуем соседний интервал."""
        second = (self.out / "interline-19" / "page.mxl", self.out / "page.log", None)
        with mock.patch.object(drums, "estimate_interline", return_value=18), \
             mock.patch.object(self.service, "_run_audiveris",
                               side_effect=[ProcessingError("NPE"), second]) as audiveris, \
             mock.patch.object(self.service, "_build_success_result",
                               return_value=FileResult(filename="page.mxl")) as build:
            result = self.service.process_single(self.photo, self.out, preset="drums_1line")

        self.assertIsNone(result.error)
        tried = [(call.args[1].name, call.args[3][-1]) for call in audiveris.call_args_list]
        self.assertEqual(tried, [
            ("interline-18", f"{drums.INTERLINE_CONSTANT}=18"),
            ("interline-19", f"{drums.INTERLINE_CONSTANT}=19"),
        ])
        build.assert_called_once_with(second[0], second[1])

    def test_one_line_drums_without_estimate_fail_plainly(self) -> None:
        with mock.patch.object(drums, "estimate_interline", return_value=None), \
             mock.patch.object(self.service, "_run_audiveris") as audiveris:
            result = self.service.process_single(self.photo, self.out, preset="drums_1line")

        audiveris.assert_not_called()
        self.assertIn("интервал", result.error or "")

    def test_drum_playlist_is_one_audiveris_book(self) -> None:
        second = self.out / "page2.png"
        second.write_bytes(b"\x89PNG\r\n\x1a\n")
        with mock.patch.object(self.service, "_run_homr_playlist") as homr_playlist, \
             mock.patch.object(self.service, "_run_audiveris_playlist",
                               return_value=self.done) as audiveris, \
             mock.patch.object(self.service, "_build_success_result",
                               return_value=FileResult(filename="page.mxl")):
            result = self.service.process_playlist([self.photo, second], self.out, preset="drums")

        self.assertIsNone(result.error)
        audiveris.assert_called_once()
        self.assertEqual(audiveris.call_args.args[0], [self.photo, second])
        homr_playlist.assert_not_called()

    def test_format_audiveris_cannot_read_is_reported(self) -> None:
        odd = self.out / "score.sib"
        odd.write_bytes(b"\x00\x01")
        with mock.patch.object(self.service, "_run_audiveris") as audiveris:
            result = self.service.process_single(odd, self.out, preset="drums")

        audiveris.assert_not_called()
        self.assertIn("не читает формат", result.error or "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
