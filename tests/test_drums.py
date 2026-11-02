"""Ударные: пресет уводит задачу в Audiveris, однолинейному стану нужен интервал.

homr ударных не знает (перкуссионный ключ читает как альтовый, крестики теряет),
поэтому `drums`/`drums_1line` идут прямо в Audiveris. Сторожится: маршрут, отказ
от отката на homr, лесенка интервалов для однолинейного стана, сама оценка
интервала по головкам, карта ударных форка (app/res/drum-set.xml) и ключи станов
в пресетах.
"""

from __future__ import annotations

import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import numpy as np

from api import drums, omr_bridge, services
from api.exceptions import ProcessingError
from api.models import FileResult
from api.presets import Preset, get_preset_args

REPO = Path(__file__).resolve().parents[1]
DRUM_SET = REPO / "app" / "res" / "drum-set.xml"
DRUM_SOUNDS = REPO / "app" / "src" / "main" / "java" / "org" / "audiveris" / "omr" / "score" / "DrumSet.java"

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


def _drum_set() -> dict[tuple[int, int, str], str]:
    """(линий в стане, позиция, головка) → звук. Записи со знаком исполнения не нужны."""
    root = ET.parse(DRUM_SET).getroot()
    return {
        (int(staff.get("line-count")), int(entry.get("pitch-position")), entry.get("motif")):
            entry.get("sound")
        for staff in root.findall("staff")
        for entry in staff.findall("entry")
        if entry.get("sign") is None
    }


class DrumSetMapTest(unittest.TestCase):
    """Карта ударных Audiveris: где искать головки и каким звуком их экспортировать.

    NoteHeadsBuilder на драм-стане ищет только формы, перечисленные для позиции, а
    позицию без записи пропускает целиком: бочка на D4 (+5) читалась крестом или
    терялась. Замер на tests/images/drum/pdmx (omr/drumcheck.py): American Idiot
    72 → 99%, Stinkfist 59 → 80%.
    """

    def setUp(self) -> None:
        self.entries = _drum_set()

    def test_sounds_are_known_to_audiveris(self) -> None:
        """Опечатка в имени звука не ошибка: загрузчик молча делает из записи удаление."""
        known = set(re.findall(r"^\s+([A-Z][A-Za-z0-9_]*)\(\d+\)[,;]", DRUM_SOUNDS.read_text(), re.M))
        self.assertIn("Acoustic_Snare", known)
        self.assertEqual(set(self.entries.values()) - known, set())

    def test_head_on_single_line_is_snare(self) -> None:
        """Hand_Clap (GM 39) звучал малым только из-за ошибки на единицу в midi-unpitched."""
        self.assertEqual(self.entries[(1, 0, "oval")], "Acoustic_Snare")
        self.assertEqual(self.entries[(1, 0, "cross")], "Side_Stick")

    def test_heads_just_outside_staff_are_searched(self) -> None:
        self.assertEqual(self.entries[(5, 5, "oval")], "Acoustic_Bass_Drum")
        self.assertIn((5, -5, "oval"), self.entries)

    def test_small_head_has_the_oval_sound(self) -> None:
        """PartwiseBuilder ищет звук малой головки как у овала на той же позиции."""
        for (lines, pitch, motif), sound in self.entries.items():
            if motif == "small":
                with self.subTest(lines=lines, pitch=pitch):
                    self.assertEqual(self.entries.get((lines, pitch, "oval")), sound)


class DrumPresetStavesTest(unittest.TestCase):
    def test_drums_reads_one_line_staves_beside_five_line(self) -> None:
        """Установка + однолинейная перкуссия на одной странице (Little Drummer Boy)."""
        args = get_preset_args(Preset.drums)
        self.assertIn("org.audiveris.omr.sheet.ProcessingSwitches.oneLineStaves=true", args)
        self.assertNotIn("org.audiveris.omr.sheet.ProcessingSwitches.fiveLineStaves=false", args)

    def test_only_drums_1line_switches_five_line_staves_off(self) -> None:
        args = get_preset_args(Preset.drums_1line)
        self.assertIn("org.audiveris.omr.sheet.ProcessingSwitches.fiveLineStaves=false", args)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
