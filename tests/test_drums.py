"""Ударные: пресет уводит задачу в Audiveris, однолинейному стану нужен интервал.

homr ударных не знает (перкуссионный ключ читает как альтовый, крестики теряет),
поэтому `drums`/`drums_1line` идут прямо в Audiveris. Сторожится: маршрут, отказ
от отката на homr, подготовка снимка (масштаб до интервала 24, свет, Otsu; порог
интервала — после масштаба), лесенка интервалов для однолинейного стана по
подготовленной странице, сама оценка интервала по головкам, повтор без
`oneLineStaves`, карта ударных форка (app/res/drum-set.xml) и ключи станов в
пресетах.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import numpy as np

from api import drums, omr_bridge, services
from api.exceptions import LowInterlineError, ProcessingError
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


def _staff_page(interline: int, width: int = 1170, staves: int = 6) -> np.ndarray:
    """Пятилинейная страница: по умолчанию — скриншот телефона, интервал 10."""
    image = np.full((int(width * 1.4), width), 255, np.uint8)
    left, right = int(width * 0.05), int(width * 0.95)
    for index in range(staves):
        top = interline * 6 + index * interline * 11
        for k in range(5):
            cv2.line(image, (left, top + k * interline), (right, top + k * interline), 0,
                     max(1, interline // 10))
        for i in range(12):
            x = int(width * 0.1) + i * int(width * 0.07)
            cy = top + (i % 5) * interline // 2
            cv2.ellipse(image, (x, cy), (int(interline * 0.65), interline // 2), -20, 0, 360, 0, -1)
            stem = x + int(interline * 0.6)
            cv2.line(image, (stem, cy), (stem, cy - int(interline * 3.5)), 0, max(1, interline // 8))
    return image


def _staff_interline(image: np.ndarray) -> float | None:
    from omr.staff import analyse

    _, staves, interline = analyse(image)
    return interline if staves else None


@unittest.skipIf(cv2 is None, "нужен OpenCV")
class PhotoPrepTest(unittest.TestCase):
    """Снимок для Audiveris: интервал около 24 px, ровный свет, только чёрное и белое."""

    def test_screenshot_is_scaled_to_the_target_interline(self) -> None:
        shot = _staff_page(10)
        interline = drums.photo_interline(shot, one_line=False)
        self.assertAlmostEqual(interline, 10, delta=0.5)
        scale, binarize = drums.photo_plan(shot.shape, interline, one_line=False)
        prepared = drums.prepare_photo(shot, scale, interline * scale, binarize)

        self.assertAlmostEqual(scale, 2.4, delta=0.1)
        self.assertEqual(set(np.unique(prepared).tolist()), {0, 255})
        self.assertAlmostEqual(_staff_interline(prepared), drums.TARGET_INTERLINE, delta=1)

    def test_scale_stays_within_audiveris_pixel_limit(self) -> None:
        """Больше 20 Мп Audiveris не берёт («Too large image»): 48-Мп фото уменьшаем."""
        for one_line in (False, True):
            with self.subTest(one_line=one_line):
                scale, _ = drums.photo_plan((8064, 6048), 30, one_line)
                self.assertLess(scale, drums.TARGET_INTERLINE / 30)
                self.assertLessEqual(8064 * 6048 * scale * scale, drums.MAX_PIXELS * 1.001)

    def test_one_line_page_of_normal_size_keeps_its_scale_and_grey(self) -> None:
        """Растяжение размывает пик толщины единственной линии: perc-01 (18 px) падал на SCALE."""
        self.assertEqual(drums.photo_plan((2299, 2142), 18, one_line=True), (1.0, False))
        scale, binarize = drums.photo_plan((1514, 1170), 10, one_line=True)
        self.assertAlmostEqual(scale, 2.4, delta=0.01)
        self.assertTrue(binarize)
        self.assertEqual(drums.photo_plan((2299, 2142), 18, one_line=False)[1], True)

    def test_grey_page_gets_only_the_light_fix(self) -> None:
        clean = _snare_line(18)
        shadow = (clean * np.linspace(0.5, 1.0, clean.shape[1])[None, :]).astype(np.uint8)
        prepared = drums.prepare_photo(shadow, 1.0, 18, binarize=False)

        self.assertEqual(prepared.shape, clean.shape)
        self.assertGreater(len(np.unique(prepared)), 2)
        paper = clean == 255
        self.assertGreater(np.percentile(prepared[paper], 5), 240)

    def test_uneven_light_is_flattened(self) -> None:
        """Тень с одного края: Otsu по сырому кадру заливает её чёрным, по выровненному — нет."""
        clean = _staff_page(24)
        shadow = (clean * np.linspace(0.35, 1.0, clean.shape[1])[None, :]).astype(np.uint8)
        _, naive = cv2.threshold(shadow, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        prepared = drums.prepare_photo(shadow, 1.0, 24)

        ink = (clean == 0).mean()
        self.assertGreater((naive == 0).mean(), ink + 0.2)
        self.assertAlmostEqual((prepared == 0).mean(), ink, delta=0.01)
        self.assertAlmostEqual(_staff_interline(prepared), 24, delta=1)

    def test_one_line_page_is_measured_by_noteheads(self) -> None:
        """Линий для разметки нет: analyse собрал бы «стан» из штрихов (137 px вместо 10)."""
        with mock.patch("omr.staff.analyse", side_effect=AssertionError("analyse не нужен")):
            self.assertAlmostEqual(drums.photo_interline(_snare_line(18), one_line=True), 18, delta=1)

    def test_page_without_staff_has_no_interline(self) -> None:
        blank = np.full((1600, 1170), 255, np.uint8)
        self.assertIsNone(drums.photo_interline(blank, one_line=False))
        self.assertIsNone(drums.photo_interline(blank, one_line=True))


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


@unittest.skipIf(cv2 is None, "нужен OpenCV")
class DrumRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        self.inbox = self.out / "in"
        self.inbox.mkdir()
        # Скриншот телефона: интервал 10, ниже прод-порога MIN_INTERLINE=11.
        self.photo = self.inbox / "page.png"
        cv2.imwrite(str(self.photo), _staff_page(10))
        self.prepared = self.out / "page.clean.png"
        self.service = services.AudiverisService()
        self.done = (self.out / "page.clean.mxl", self.out / "page.log", 24)
        floor = mock.patch.object(services.settings, "min_interline", 11)
        floor.start()
        self.addCleanup(floor.stop)

    def _built(self):
        return mock.patch.object(self.service, "_build_success_result",
                                 return_value=FileResult(filename="page.clean.mxl"))

    def test_drums_go_straight_to_audiveris(self) -> None:
        with mock.patch.object(omr_bridge, "run") as omr, \
             mock.patch.object(services.homr_service, "run") as homr, \
             mock.patch.object(self.service, "_run_audiveris", return_value=self.done) as audiveris, \
             self._built():
            result = self.service.process_single(self.photo, self.out, preset="drums")

        self.assertIsNone(result.error)
        audiveris.assert_called_once()
        page, target, preset, extra, _ = audiveris.call_args.args
        self.assertEqual((page, target, preset, list(extra)), (self.prepared, self.out, "drums", []))
        omr.assert_not_called()
        homr.assert_not_called()

    def test_small_screenshot_is_scaled_before_the_interline_floor(self) -> None:
        """Интервал 10 < 11 — раньше отказ до распознавания; теперь Audiveris видит 24."""
        with mock.patch.object(self.service, "_run_audiveris", return_value=self.done) as audiveris, \
             self._built():
            result = self.service.process_single(self.photo, self.out, preset="drums")

        self.assertIsNone(result.error)
        audiveris.assert_called_once()
        prepared = cv2.imread(str(audiveris.call_args.args[0]), cv2.IMREAD_GRAYSCALE)
        self.assertAlmostEqual(_staff_interline(prepared), drums.TARGET_INTERLINE, delta=1)
        self.assertTrue(self.photo.exists(), "исходный снимок нужен архиву провалов")

    def test_interline_floor_applies_after_scaling(self) -> None:
        """Не растянуть до порога (лимит пикселей Audiveris) — понятный отказ без Audiveris."""
        with mock.patch.object(drums, "MAX_PIXELS", 600_000), \
             mock.patch.object(self.service, "_run_audiveris") as audiveris:
            result = self.service.process_single(self.photo, self.out, preset="drums")

        audiveris.assert_not_called()
        self.assertIn("слишком мелкий", result.error or "")
        self.assertIn("11px", result.error or "")

    def test_page_without_staff_fails_plainly(self) -> None:
        cv2.imwrite(str(self.photo), np.full((1600, 1170), 255, np.uint8))
        with mock.patch.object(self.service, "_run_audiveris") as audiveris:
            result = self.service.process_single(self.photo, self.out, preset="drums")

        audiveris.assert_not_called()
        self.assertIn("интервал", result.error or "")

    def test_pdf_goes_to_audiveris_as_is(self) -> None:
        """PDF Audiveris рисует сам на 300 DPI и читает на 95-100% — не трогаем."""
        pdf = self.inbox / "score.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        with mock.patch.object(self.service, "_run_audiveris", return_value=self.done) as audiveris, \
             self._built():
            self.service.process_single(pdf, self.out, preset="drums")

        self.assertEqual(audiveris.call_args.args[0], pdf)
        self.assertFalse(list(self.out.glob("*.clean.png")))

    def test_tab_crash_is_retried_without_one_line_staves(self) -> None:
        """TAB + oneLineStaves = NegativeArraySizeException (mix-under-the-bridge)."""
        second = (self.out / "five-line-only" / "page.clean.mxl", self.out / "page.log", 24)
        with mock.patch.object(self.service, "_run_audiveris",
                               side_effect=[ProcessingError("NegativeArraySizeException"), second]
                               ) as audiveris, \
             self._built() as build:
            result = self.service.process_single(self.photo, self.out, preset="drums")

        self.assertIsNone(result.error)
        tried = [(call.args[1], list(call.args[3])) for call in audiveris.call_args_list]
        self.assertEqual(tried, [
            (self.out, []),
            (self.out / "five-line-only", ["-constant", drums.NO_ONE_LINE_STAVES]),
        ])
        build.assert_called_once_with(second[0], second[1])

    def test_low_interline_is_not_retried(self) -> None:
        with mock.patch.object(self.service, "_run_audiveris",
                               side_effect=LowInterlineError(8, "interline=8px", self.out / "b.log")
                               ) as audiveris:
            result = self.service.process_single(self.photo, self.out, preset="drums")

        audiveris.assert_called_once()
        self.assertIn("interline=8px", result.error or "")

    def test_one_line_override_follows_the_preset_constant(self) -> None:
        """Повтор `-constant` перекрывает ключ пресета, только если стоит после него."""
        with mock.patch.object(self.service, "_execute_and_process", return_value=self.done) as run:
            self.service._run_audiveris(self.photo, self.out, "drums",
                                        ["-constant", drums.NO_ONE_LINE_STAVES])
        cmd = run.call_args.args[0]
        self.assertLess(cmd.index("org.audiveris.omr.sheet.ProcessingSwitches.oneLineStaves=true"),
                        cmd.index(drums.NO_ONE_LINE_STAVES))

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
        second = (self.out / "interline-19" / "page.clean.mxl", self.out / "page.log", None)
        with mock.patch.object(drums, "estimate_interline", return_value=18), \
             mock.patch.object(self.service, "_run_audiveris",
                               side_effect=[ProcessingError("NPE"), second]) as audiveris, \
             self._built() as build:
            result = self.service.process_single(self.photo, self.out, preset="drums_1line")

        self.assertIsNone(result.error)
        tried = [(call.args[1].name, call.args[3][-1]) for call in audiveris.call_args_list]
        self.assertEqual(tried, [
            ("interline-18", f"{drums.INTERLINE_CONSTANT}=18"),
            ("interline-19", f"{drums.INTERLINE_CONSTANT}=19"),
        ])
        build.assert_called_once_with(second[0], second[1])

    def test_one_line_ladder_is_measured_on_the_prepared_page(self) -> None:
        """Мелкий снимок (головки 14 px): Audiveris получает страницу и интервал после масштаба."""
        cv2.imwrite(str(self.photo), _snare_line(14))
        with mock.patch.object(self.service, "_run_audiveris", return_value=self.done) as audiveris, \
             self._built():
            result = self.service.process_single(self.photo, self.out, preset="drums_1line")

        self.assertIsNone(result.error)
        page, target, _, extra, _ = audiveris.call_args.args
        self.assertEqual(page, self.prepared)
        value = int(extra[-1].rsplit("=", 1)[1])
        self.assertEqual(target.name, f"interline-{value}")
        self.assertAlmostEqual(value, drums.TARGET_INTERLINE, delta=3)
        self.assertEqual(value, drums.estimate_interline(cv2.imread(str(page), cv2.IMREAD_GRAYSCALE)))

    def test_one_line_photo_of_normal_size_is_not_scaled(self) -> None:
        """Головки 18 px — рабочее окно Audiveris: только свет, без растяжения и Otsu."""
        page = _snare_line(18)
        cv2.imwrite(str(self.photo), page)
        with mock.patch.object(self.service, "_run_audiveris", return_value=self.done) as audiveris, \
             self._built():
            result = self.service.process_single(self.photo, self.out, preset="drums_1line")

        self.assertIsNone(result.error)
        prepared = cv2.imread(str(audiveris.call_args.args[0]), cv2.IMREAD_GRAYSCALE)
        self.assertEqual(prepared.shape, page.shape)
        value = int(audiveris.call_args.args[3][-1].rsplit("=", 1)[1])
        self.assertAlmostEqual(value, 18, delta=1)

    def test_one_line_drums_are_not_retried_without_one_line_staves(self) -> None:
        """У drums_1line других станов нет: провал ведёт по лесенке, а не к выключению ключа."""
        with mock.patch.object(drums, "estimate_interline", return_value=18), \
             mock.patch.object(self.service, "_run_audiveris",
                               side_effect=ProcessingError("NPE")) as audiveris:
            result = self.service.process_single(self.photo, self.out, preset="drums_1line")

        self.assertEqual(result.error, "NPE")
        for call in audiveris.call_args_list:
            self.assertNotIn(drums.NO_ONE_LINE_STAVES, call.args[3])

    def test_one_line_drums_without_estimate_fail_plainly(self) -> None:
        with mock.patch.object(drums, "estimate_interline", return_value=None), \
             mock.patch.object(self.service, "_run_audiveris") as audiveris:
            result = self.service.process_single(self.photo, self.out, preset="drums_1line")

        audiveris.assert_not_called()
        self.assertIn("интервал", result.error or "")

    def test_drum_playlist_is_one_audiveris_book(self) -> None:
        second = self.inbox / "page2.png"
        cv2.imwrite(str(second), _staff_page(10))
        with mock.patch.object(self.service, "_run_homr_playlist") as homr_playlist, \
             mock.patch.object(self.service, "_run_audiveris_playlist",
                               return_value=self.done) as audiveris, \
             self._built():
            result = self.service.process_playlist([self.photo, second], self.out, preset="drums")

        self.assertIsNone(result.error)
        audiveris.assert_called_once()
        self.assertEqual(audiveris.call_args.args[0],
                         [self.out / "0.page.clean.png", self.out / "1.page2.clean.png"])
        homr_playlist.assert_not_called()

    def test_format_audiveris_cannot_read_is_reported(self) -> None:
        odd = self.inbox / "score.sib"
        odd.write_bytes(b"\x00\x01")
        with mock.patch.object(self.service, "_run_audiveris") as audiveris:
            result = self.service.process_single(odd, self.out, preset="drums")

        audiveris.assert_not_called()
        self.assertIn("не читает формат", result.error or "")


class EmptySheetTest(unittest.TestCase):
    """Обложка без нот роняет всю книгу Audiveris; разобранные листы экспортируем повтором."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        self.book = self.out / "score.omr"
        self.cmd = ["audiveris", "-batch", "-transcribe", "-export", "-output", str(self.out),
                    str(self.out / "in" / "score.pdf")]
        self.service = services.AudiverisService()

    def _audiveris(self, first_stdout: str):
        def run(cmd, output_dir, timeout):
            if cmd[-1] == str(self.book):
                (self.out / "score.mxl").write_bytes(b"PK")
                return subprocess.CompletedProcess(cmd, 0, "exported", "")
            self.book.write_bytes(b"PK")
            return subprocess.CompletedProcess(cmd, 1, first_stdout, "")
        return mock.patch.object(self.service, "_run_command", side_effect=run)

    def test_book_is_exported_without_the_empty_sheet(self) -> None:
        stdout = "INFO Sheet score#1 flagged as invalid.\nWARN Exception on score, Error in reaching step PAGE"
        with self._audiveris(stdout) as run:
            output, _, _ = self.service._execute_and_process(self.cmd, self.out, 120, book=self.book)

        self.assertEqual(output, self.out / "score.mxl")
        self.assertEqual([call.args[0][-1] for call in run.call_args_list],
                         [self.cmd[-1], str(self.book)])
        self.assertEqual(run.call_args_list[1].args[0][:-1], self.cmd[:-1])

    def test_crash_without_empty_sheet_is_not_repeated(self) -> None:
        """TAB-падение листов не помечает — его лечит повтор без oneLineStaves."""
        with self._audiveris("WARN Error processing stub java.lang.NegativeArraySizeException") as run:
            with self.assertRaises(ProcessingError):
                self.service._execute_and_process(self.cmd, self.out, 120, book=self.book)

        run.assert_called_once()

    def test_single_drum_run_points_at_its_book(self) -> None:
        with mock.patch.object(self.service, "_execute_and_process",
                               return_value=(self.out / "score.mxl", None, None)) as run:
            self.service._run_audiveris(self.out / "in" / "score.png", self.out, "drums")
        self.assertEqual(run.call_args.kwargs["book"], self.out / "score.omr")


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

    def test_only_drums_retries_without_one_line_staves(self) -> None:
        self.assertTrue(drums.reads_one_line_staves("drums"))
        self.assertFalse(drums.reads_one_line_staves("drums_1line"))
        self.assertFalse(drums.reads_one_line_staves("default"))


class DrumPresetGraceTest(unittest.TestCase):
    def test_drum_presets_read_grace_notes(self) -> None:
        """Форшлаг — малая головка; без smallHeads Audiveris его не видит (Drummer's Delight)."""
        for preset in (Preset.drums, Preset.drums_1line):
            with self.subTest(preset=preset):
                self.assertIn("org.audiveris.omr.sheet.ProcessingSwitches.smallHeads=true",
                              get_preset_args(preset))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
