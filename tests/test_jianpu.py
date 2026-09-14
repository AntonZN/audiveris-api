"""Цзянпу: модель мелодии, обёртка движка jpeditor и маршрут пресета `jianpu`.

Движок в тестах подменён скриптом на Python (`OMR_NODE` — этот же интерпретатор):
сторожится обёртка — раскладка выходов по страницам, провал одной страницы,
склейка, EXIF, — а не само распознавание. Его меряет `python -m omr.jianpu.bench`
на эталонах `tests/images/jianpu/synth`.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from unittest import mock

from api import jianpu, omr_bridge, services
from api.exceptions import ProcessingError
from api.models import FileResult
from omr.jianpu import melody
from omr.jianpu.melody import Measure, Melody, Note

try:
    import cv2
    import numpy as np

    from omr.engines import jianpu_engine
    from omr.engines.homr_engine import EngineError
    from omr.engines.jianpu_engine import HeaderLine
    from omr.jianpu import tempo, voices
    from omr.jianpu.recognize import JianpuResult, _readable, recognize
except ImportError:  # pragma: no cover
    cv2 = None

QUARTER = Fraction(1)
EIGHTH = Fraction(1, 2)


def _note(step: str, octave: int = 4, duration: Fraction = QUARTER, **kwargs) -> Note:
    return Note(Fraction(duration), step, octave=octave, **kwargs)


class MelodyTest(unittest.TestCase):
    def test_tonic_octave_follows_the_engine(self) -> None:
        """Октаву лист не задаёт; договорённость — как у jpeditor: буква тоники B — 3-я октава."""
        self.assertEqual(melody.tonic_diatonic(0), _note("C", 4).diatonic)
        self.assertEqual(melody.tonic_diatonic(1), _note("G", 4).diatonic)
        self.assertEqual(melody.tonic_diatonic(-2), _note("B", 3).diatonic)

    def test_meter_is_what_the_measure_holds(self) -> None:
        """Anthology пишет 4/4 в каждом такте при фактических 2/4 — рисовать надо факт."""
        song = Melody("t", [
            Measure([_note("G", duration=EIGHTH)], time=(4, 4)),
            Measure([_note("C"), _note("D")]),
            Measure([_note("E"), _note("D")]),
            Measure([_note("C"), _note("D"), _note("E")]),
        ])
        melody.fix_meters(song)
        self.assertTrue(song.measures[0].pickup)
        self.assertEqual([m.time for m in song.measures], [(2, 4), None, None, (3, 4)])

    def test_half_a_tie_is_dropped(self) -> None:
        """jianpu-ly рисует лигу по началу, а эталон сливал бы звук по концу."""
        first, second, third = _note("C", tie_start=True), _note("D"), _note("D", tie_stop=True)
        melody.pair_ties(Melody("t", [Measure([first, second, third])]))
        self.assertFalse(first.tie_start or second.tie_stop or third.tie_stop)

    def test_long_note_becomes_tied_glyphs(self) -> None:
        song = Melody("t", [Measure([_note("G", duration=Fraction(5, 2)), _note("E", duration=EIGHTH)],
                                    time=(3, 4), key=0)])
        melody.split_glyphs(song)
        self.assertEqual([n.duration for n in song.measures[0].notes], [2, EIGHTH, EIGHTH])
        self.assertIn("5 - ~ q5 q3 |", melody.to_jly(song))

    def test_accidentals_and_dots_count_from_the_key(self) -> None:
        """1=Bb: ми-бекар — «#4», си-бемоль первой октавы — «1'» (тоника Bb3)."""
        song = Melody("t", [Measure([Note(QUARTER, "E", 0, 4), Note(QUARTER, "B", -1, 4)],
                                    time=(2, 4), key=-2)])
        self.assertIn("1=Bb 2/4 #4 1' |", melody.to_jly(song))

    def test_reference_round_trips_through_musicxml(self) -> None:
        """Эталон читается тем же читателем, что и источники: ноты, лиги, слоги, размеры."""
        song = Melody("t", [
            Measure([_note("G", 3, EIGHTH, lyrics={1: "月"})], time=(4, 4), key=0),
            Measure([_note("C", lyrics={1: "亮"}), _note("E", duration=Fraction(5, 2), lyrics={1: "挂"}),
                     _note("D", duration=EIGHTH)]),
            Measure([_note("C", duration=2)]),
        ])
        melody.prepare(song)
        back = melody.read_musicxml(ET.fromstring(melody.to_musicxml(song)))

        def shape(tune: Melody) -> list:
            return [(n.duration, n.step, n.alter, n.octave, n.tie_start, n.tie_stop, n.lyrics)
                    for n in tune.notes()]

        self.assertEqual(shape(back), shape(song))
        self.assertEqual([m.time for m in back.measures], [m.time for m in song.measures])


# Подмена раннера: на каждую картинку пишет MusicXML и заголовок из FAKE_HEADER,
# кроме названной в FAKE_FAIL, — про неё говорит так же, как настоящий раннер.
_FAKE_ENGINE = r'''
import os, pathlib, sys
omr_js, out, *images = sys.argv[1:]
out = pathlib.Path(out)
failed = 0
for arg in images:
    image = pathlib.Path(arg)
    if image.name == os.environ.get("FAKE_FAIL"):
        print(f"✗ {image.name}: 未找到谱行", file=sys.stderr)
        failed += 1
        continue
    (out / (image.stem + ".musicxml")).write_text(MUSICXML, encoding="utf-8")
    (out / (image.stem + ".header.json")).write_text(os.environ.get("FAKE_HEADER", "[]"), encoding="utf-8")
sys.exit(1 if failed else 0)
'''

_PAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.0"><part-list><score-part id="P1"><part-name/></score-part></part-list>
<part id="P1"><measure number="1"><attributes><divisions>1</divisions><key><fifths>0</fifths></key>
<time><beats>1</beats><beat-type>4</beat-type></time></attributes>
<note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note>
</measure></part></score-partwise>
"""


@unittest.skipIf(cv2 is None, "нужен OpenCV")
class EngineWrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        script = self.dir / "fake-runner.py"
        script.write_text(f"MUSICXML = {_PAGE_XML!r}\n" + _FAKE_ENGINE, encoding="utf-8")
        package = self.dir / "package"
        package.mkdir()
        (package / "omr.js").write_text("", encoding="utf-8")
        environment = mock.patch.dict(os.environ, {
            "OMR_NODE": sys.executable, "OMR_JIANPU_RUNNER": str(script),
            "OMR_JIANPU_PACKAGE": str(package), "PYTHONIOENCODING": "utf-8",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def _image(self, name: str) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), np.full((40, 60, 3), 255, np.uint8))
        return path

    def test_failed_page_is_reported_and_the_rest_are_kept(self) -> None:
        pages = [(self._image(f"image{i}.png"), f"song.p{i:02d}") for i in (1, 2, 3)]
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "page002.png"}):
            result = jianpu_engine.run_pages(pages, self.dir / "out")
        self.assertEqual(sorted(result.outputs), ["song.p01", "song.p03"])
        self.assertEqual(result.errors, {"song.p02": "未找到谱行"})
        self.assertTrue((self.dir / "out" / "song.p03.musicxml").exists())
        self.assertFalse((self.dir / "out" / ".jianpu").exists())

    def test_same_names_from_different_folders_do_not_collide(self) -> None:
        """Выход движок называет по имени входа — два image0.png затёрли бы друг друга."""
        pages = [(self._image("a/image0.png"), "x.p01"), (self._image("b/image0.png"), "x.p02")]
        result = jianpu_engine.run_pages(pages, self.dir / "out")
        self.assertEqual(sorted(result.outputs), ["x.p01", "x.p02"])

    def test_missing_engine_is_an_engine_error(self) -> None:
        with mock.patch.dict(os.environ, {"OMR_JIANPU_PACKAGE": str(self.dir / "nope")}):
            with self.assertRaises(EngineError):
                jianpu_engine.run_pages([(self._image("p.png"), "p")], self.dir / "out")

    def test_pages_of_one_song_are_merged(self) -> None:
        result = recognize([self._image("1.png"), self._image("2.png")], self.dir / "out",
                           stem="playlist")
        self.assertEqual(result.musicxml, self.dir / "out" / "playlist.musicxml")
        self.assertEqual(len(ET.parse(result.musicxml).getroot().findall("part/measure")), 2)
        self.assertEqual(result.recognised, 2)

    def test_photo_rotated_by_exif_is_given_upright(self) -> None:
        """sharp в движке EXIF не применяет — снимок «на боку с пометкой» пришёл бы боком."""
        from PIL import Image

        photo = self.dir / "phone.jpg"
        exif = Image.Exif()
        exif[0x0112] = 6
        Image.new("RGB", (60, 40), "white").save(photo, exif=exif)
        upright = _readable(photo, self.dir / "work")
        self.assertEqual(upright.suffix, ".png")
        self.assertEqual(cv2.imread(str(upright)).shape[:2], (60, 40))
        plain = self._image("plain.png")
        self.assertEqual(_readable(plain, self.dir / "work"), plain)

    def test_choir_is_refused_before_the_engine(self) -> None:
        """Движок слил бы голоса в одну партию, и это ушло бы клиенту как успех."""
        choir = self.dir / "choir.png"
        cv2.imwrite(str(choir), _choir_page())
        result = recognize([choir], self.dir / "out")
        self.assertIsNone(result.musicxml)
        self.assertIn("многоголосие", result.refused)
        self.assertFalse((self.dir / "out" / "choir.engine.log").exists())

    def test_tempo_word_from_the_header_reaches_the_score(self) -> None:
        """Строку «中速 深情地» движок выбрасывает — темп берётся из перехваченного заголовка."""
        header = '[{"text": "中速深情地", "height": 31, "x": 154, "y": 125}]'
        with mock.patch.dict(os.environ, {"FAKE_HEADER": header}):
            result = recognize([self._image("song.png")], self.dir / "out")
        self.assertEqual(ET.parse(result.musicxml).getroot().find(".//sound").get("tempo"), "88")
        self.assertTrue(any("оценка по слову «中速»" in note for note in result.notes))


def _digits_row(page: np.ndarray, y: int, x: int = 160) -> None:
    """Строка «5 3 2 1 | 5 3 2 1 | …» с тактовыми чертами в её высоту."""
    for _ in range(4):
        for digit in "5321":
            cv2.putText(page, digit, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
            x += 48
        cv2.line(page, (x - 8, y - 30), (x - 8, y + 6), (0, 0, 0), 2)
        x += 24


def _single_voice_page() -> np.ndarray:
    """Одна мелодия: строка цифр, под ней строка текста — шесть раз."""
    page = np.full((1600, 1300, 3), 255, np.uint8)
    for row in range(6):
        y = 160 + row * 230
        _digits_row(page, y)
        cv2.putText(page, "la la la la la la la la", (160, y + 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
    return page


def _choir_page() -> np.ndarray:
    """Хор: две системы по четыре голоса, голоса объединены скобкой «[» слева."""
    page = np.full((1600, 1300, 3), 255, np.uint8)
    for system in range(2):
        top = 150 + system * 700
        rows = [top + 40 + voice * 140 for voice in range(4)]
        for label, y in zip("SATB", rows):
            cv2.putText(page, label, (60, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
            _digits_row(page, y)
            cv2.putText(page, "la la la la la la", (160, y + 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
        bottom = rows[-1] + 20
        for start, end in (((120, top), (120, bottom)), ((120, top), (140, top)),
                           ((120, bottom), (140, bottom))):
            cv2.line(page, start, end, (0, 0, 0), 5)
    return page


@unittest.skipIf(cv2 is None, "нужен OpenCV")
class VoicesTest(unittest.TestCase):
    def test_single_voice_is_not_refused(self) -> None:
        self.assertFalse(voices.detect(_single_voice_page()).multi)

    def test_choir_bracket_is_found_in_every_system(self) -> None:
        report = voices.detect(_choir_page())
        self.assertTrue(report.multi)
        self.assertEqual(len(report.brackets), 2)

    def test_frame_around_text_is_not_a_bracket(self) -> None:
        """Рамка вокруг текста: у левой стороны есть парная вертикаль правее."""
        page = _single_voice_page()
        cv2.line(page, (130, 100), (130, 700), (0, 0, 0), 3)
        cv2.line(page, (1100, 100), (1100, 700), (0, 0, 0), 3)
        self.assertFalse(voices.detect(page).multi)

    def test_photo_edge_is_not_a_bracket(self) -> None:
        """Тёмный край кадра на фото книги — вертикаль со всем текстом справа."""
        page = _single_voice_page()
        page[200:900, :16] = 0
        self.assertFalse(voices.detect(page).multi)


@unittest.skipIf(cv2 is None, "нужен OpenCV")
class TempoTest(unittest.TestCase):
    def test_textbook_scale_and_plain_words(self) -> None:
        cases = {"中速 深情地": 88, "小快板": 108, "快板": 132, "慢速": 52, "稍快": 108, "Allegro": 132}
        for text, bpm in cases.items():
            with self.subTest(text=text):
                self.assertEqual(tempo.from_header(["1=C", text]).bpm, bpm)

    def test_metronome_beats_the_word_and_free_rhythm_has_no_tempo(self) -> None:
        self.assertEqual(tempo.from_header(["中速 ♩=72"]).bpm, 72)
        self.assertIsNone(tempo.from_header(["散板"]).bpm)
        self.assertIsNone(tempo.from_header(["1=C", "作词：邱字林", "3", "4"]))

    def _score(self, title: str) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "score.musicxml"
        path.write_text(_PAGE_XML.replace("<part-list>", f"<work><work-title>{title}</work-title></work><part-list>"),
                        encoding="utf-8")
        return path

    def test_title_taken_by_the_tempo_line_is_given_back(self) -> None:
        """Крупную строку «中速深情地» движок делает названием вместо «梦里故乡»."""
        path = self._score("中速深情地")
        tempo.apply(path, [HeaderLine("梦里故乡", 34, 629, 33), HeaderLine("1=C", 26, 118, 74),
                           HeaderLine("作词：邱字林", 32, 1085, 75), HeaderLine("中速深情地", 31, 154, 125)])
        root = ET.parse(path).getroot()
        self.assertEqual(root.findtext("work/work-title"), "梦里故乡")
        self.assertEqual(root.find(".//sound").get("tempo"), "88")

    def test_tempo_word_inside_a_real_title_is_left_alone(self) -> None:
        """«如歌的行板» — название пьесы, а не указание темпа."""
        path = self._score("如歌的行板")
        notes = tempo.apply(path, [HeaderLine("如歌的行板", 34, 600, 30), HeaderLine("1=D", 26, 118, 74)])
        root = ET.parse(path).getroot()
        self.assertEqual(root.findtext("work/work-title"), "如歌的行板")
        self.assertIsNone(root.find(".//sound"))
        self.assertIn("темп: на листе не указан", notes)


class JianpuRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name)
        self.photo = self.out / "page.png"
        # Содержимое неважно: движки замоканы, файл нужен как путь.
        self.photo.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.service = services.AudiverisService()
        self.done = (self.out / "page.musicxml", self.out / "page.jianpu.log")

    def test_jianpu_goes_straight_to_its_engine(self) -> None:
        with mock.patch.object(omr_bridge, "run") as omr, \
             mock.patch.object(services.homr_service, "run") as homr, \
             mock.patch.object(jianpu, "run", return_value=self.done) as engine, \
             mock.patch.object(self.service, "_run_audiveris") as audiveris, \
             mock.patch.object(self.service, "_build_success_result",
                               return_value=FileResult(filename="page.musicxml")) as build:
            result = self.service.process_single(self.photo, self.out, preset="jianpu")

        self.assertIsNone(result.error)
        engine.assert_called_once_with([self.photo], self.out, "page")
        build.assert_called_once_with(*self.done)
        omr.assert_not_called()
        homr.assert_not_called()
        audiveris.assert_not_called()

    def test_failed_jianpu_does_not_fall_back_to_homr(self) -> None:
        """homr цифр не читает: «успешная» партитура из его нот хуже честной ошибки."""
        with mock.patch.object(services.homr_service, "run") as homr, \
             mock.patch.object(jianpu, "run",
                               side_effect=ProcessingError("Цзянпу не распознан: 未找到谱行")):
            result = self.service.process_single(self.photo, self.out, preset="jianpu")

        self.assertEqual(result.error, "Цзянпу не распознан: 未找到谱行")
        homr.assert_not_called()

    def test_jianpu_playlist_is_one_song(self) -> None:
        second = self.out / "page2.png"
        second.write_bytes(b"\x89PNG\r\n\x1a\n")
        with mock.patch.object(self.service, "_run_homr_playlist") as homr_playlist, \
             mock.patch.object(jianpu, "run", return_value=self.done) as engine, \
             mock.patch.object(self.service, "_build_success_result",
                               return_value=FileResult(filename="playlist.musicxml")):
            result = self.service.process_playlist([self.photo, second], self.out, preset="jianpu")

        self.assertIsNone(result.error)
        engine.assert_called_once_with([self.photo, second], self.out, "playlist")
        homr_playlist.assert_not_called()

    @unittest.skipIf(cv2 is None, "нужен OpenCV")
    def test_refusal_reason_reaches_the_client(self) -> None:
        refused = JianpuResult(None, refused="многоголосие на стр.1 (скобок голосов 3)")
        with mock.patch("omr.jianpu.recognize.recognize", return_value=refused):
            with self.assertRaises(ProcessingError) as caught:
                jianpu.run([self.photo], self.out, "page")
        self.assertIn("многоголосие на стр.1", caught.exception.message)


@unittest.skipIf(cv2 is None, "нужен OpenCV")
class RefcheckSkipsJianpuTest(unittest.TestCase):
    def test_jianpu_folders_are_not_fed_to_the_staff_engine(self) -> None:
        from omr.refcheck import collect

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("jianpu/synth/a.pdf", "bass/b.pdf"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(b"%PDF-1.4")
            found = [path.relative_to(root).as_posix() for path in collect([temp])]
        self.assertEqual(found, ["bass/b.pdf"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
