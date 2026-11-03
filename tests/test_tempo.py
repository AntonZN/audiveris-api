"""Тесты источников BPM, кроме самого движка.

homr темп из картинки не читает вообще: `<sound tempo>` он умеет только
подставить из аргумента командной строки. Зато метрономную строку он OCR-ит над
верхним станом и выбрасывает как «не заголовок». Здесь проверяется вся цепочка,
которая эту строку подбирает, — она заменяет отдельный запуск Audiveris ради
одного числа.
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

from api.analysis import bpm_from_texts, collect_bpm
from api.models import ScoreTexts, TextDirection
from api.services import AudiverisService

SCORE = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>P1</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>4</divisions></attributes>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration>
      <type>quarter</type></note>
  </measure></part>
</score-partwise>
"""


class BpmFromTextsTest(unittest.TestCase):
    def test_reads_the_usual_metronome_marks(self) -> None:
        for text, expected in [
            ("♩ = 95", 95),
            ("♩=95", 95),
            ("J=120", 120),            # OCR часто теряет нотную голову
            ("=88", 88),
            ("95 BPM", 95),
            ("bpm: 76", 76),
            ("M.M. ♩ = 88", 88),
            ("Allegro ♩=120", 120),    # отметка, сросшаяся с заголовком
            ('♩ = 117 "Water"', 117),  # и она же, слипшаяся в одну строку OCR
        ]:
            with self.subTest(text=text):
                self.assertEqual(bpm_from_texts([text]), expected)

    def test_takes_the_lower_bound_of_a_range(self) -> None:
        """«♩=100-120»: играть в нижней границе безопаснее, чем в верхней."""
        self.assertEqual(bpm_from_texts(["♩=100-120"]), 100)

    def test_ignores_numbers_that_are_not_tempo(self) -> None:
        """Без «=» или «BPM» рядом число не темп — иначе им станет год или опус."""
        for text in ["1995", "4/4", "Op. 27", "© 2019 Universal", "Adagio", "page 12", ""]:
            with self.subTest(text=text):
                self.assertIsNone(bpm_from_texts([text]))

    def test_rejects_implausible_values(self) -> None:
        # «♩=1200» без границы по цифрам дало бы 120 — ровно эта ошибка и была.
        for text in ["♩=1200", "♩=9", "1995 BPM"]:
            with self.subTest(text=text):
                self.assertIsNone(bpm_from_texts([text]))

    def test_takes_the_first_plausible_of_several_strings(self) -> None:
        self.assertEqual(bpm_from_texts(["Op. 27", "♩=72", "♩=144"]), 72)


class TempoCandidatesTest(unittest.TestCase):
    def test_title_comes_first(self) -> None:
        texts = ScoreTexts(title="Allegro ♩=120", credits=["♩=60"])
        self.assertEqual(bpm_from_texts(AudiverisService._tempo_candidates(texts)), 120)

    def test_takes_directions_too(self) -> None:
        texts = ScoreTexts(directions=[TextDirection(text="♩ = 84", measure="1")])
        self.assertEqual(bpm_from_texts(AudiverisService._tempo_candidates(texts)), 84)

    def test_skips_lyrics_and_part_names(self) -> None:
        """Там темпа не бывает, а числа бывают — «Violin 2», слог «95»."""
        texts = ScoreTexts(part_names=["Violin =2"], instrument_names=["Piano =88"])
        self.assertEqual(AudiverisService._tempo_candidates(texts), [])


class BridgeTempoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.score = Path(self._tmp.name) / "page.musicxml"
        self.score.write_text(SCORE)

    def test_injects_the_tempo_read_from_discarded_ocr(self) -> None:
        from api.omr_bridge import _apply_tempo

        note = _apply_tempo(self.score, ["♩=95", "3"])

        self.assertEqual(collect_bpm(self.score), 95)
        self.assertIn("95", note)

    def test_keeps_the_tempo_the_engine_already_found(self) -> None:
        """Движок темп нашёл — свой разбор OCR не навязываем."""
        from api.analysis import inject_bpm
        from api.omr_bridge import _apply_tempo

        inject_bpm(self.score, 60)
        _apply_tempo(self.score, ["♩=95"])

        self.assertEqual(collect_bpm(self.score), 60)

    def test_says_so_when_there_is_nothing_to_read(self) -> None:
        from api.omr_bridge import _apply_tempo

        self.assertIsNone(collect_bpm(self.score))
        self.assertIn("не отбросил", _apply_tempo(self.score, []))
        self.assertIsNone(collect_bpm(self.score))


class BridgeFailureMessageTest(unittest.TestCase):
    def test_reason_is_short_and_free_of_server_paths(self) -> None:
        """Провал многостраничного PDF повторял одну причину на каждую страницу
        и тащил в ответ API абсолютные пути сервера."""
        from types import SimpleNamespace

        from api.omr_bridge import _failure_reason

        pages = [
            SimpleNamespace(error="", skipped="нотных станов не найдено", prepare=None),
            SimpleNamespace(error="homr не дал MusicXML (код 1), см. /srv/out/p02.log",
                            skipped="", prepare=None),
            SimpleNamespace(error="homr не дал MusicXML (код 1), см. /srv/out/p03.log",
                            skipped="", prepare=None),
        ]
        reason = _failure_reason(SimpleNamespace(pages=pages))

        self.assertEqual(reason, "нотных станов не найдено; homr не дал MusicXML (код 1)")
        self.assertNotIn("/srv", reason)

    def test_puts_the_scale_note_before_the_engine_crash(self) -> None:
        """Интервал вне рабочего коридора — причина, падение движка — следствие.
        Замерено на боевом файле: крупный план одного стана, 186px на интервал."""
        from types import SimpleNamespace

        from api.failure_reasons import FailureReason, classify
        from api.omr_bridge import _failure_reason

        page = SimpleNamespace(
            error="homr не дал MusicXML (код 1), см. /srv/out/page.log",
            skipped="",
            prepare=SimpleNamespace(stages=[
                SimpleNamespace(name="deskew", detail="уже ровно"),
                SimpleNamespace(name="normalize",
                                detail="стан слишком крупный (186.5px на интервал)"),
            ]),
        )
        reason = _failure_reason(SimpleNamespace(pages=[page]))

        self.assertTrue(reason.startswith("стан слишком крупный"), reason)
        self.assertEqual(classify(reason), FailureReason.BAD_SCALE.value)

    def test_falls_back_to_a_plain_phrase(self) -> None:
        from types import SimpleNamespace

        from api.omr_bridge import _failure_reason

        self.assertEqual(_failure_reason(SimpleNamespace(pages=[])), "нет страниц с нотами")


class RunnerCaptureTest(unittest.TestCase):
    """Перехват в раннере: строки, которые homr забраковал, ложатся в сайдкар."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.image = Path(self._tmp.name) / "page.png"

        # Подставляем модуль homr.title_detection: настоящий тут не нужен, важна
        # только точка перехвата и её контракт (True = строка отбракована).
        def is_tempo_marking(text: str) -> bool:
            return sum(1 for c in text if "a" <= c.lower() <= "z") < 4

        stub = types.ModuleType("homr.title_detection")
        stub.is_tempo_marking = is_tempo_marking
        package = types.ModuleType("homr")
        package.title_detection = stub
        self._saved = {name: sys.modules.get(name) for name in ("homr", "homr.title_detection")}
        sys.modules["homr"] = package
        sys.modules["homr.title_detection"] = stub
        self.addCleanup(self._restore)
        self.stub = stub

    def _restore(self) -> None:
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_records_every_line_and_keeps_the_verdict(self) -> None:
        """Пишем и принятые строки тоже: отметка бывает слипшейся с текстом
        («♩ = 117 "Water"»), такую строку homr принимает — и «=» из неё вырезает
        `cleanup_text`. Вердикт самой функции при этом остаётся как был."""
        from omr.engines._homr_runner import _install_tempo_capture

        _install_tempo_capture(str(self.image))

        self.assertTrue(self.stub.is_tempo_marking("♩=95"))      # вердикт не меняем
        self.assertFalse(self.stub.is_tempo_marking('= 117 "Water"'))

        sidecar = self.image.with_suffix(".ocr.txt")
        self.assertEqual(
            sidecar.read_text(encoding="utf-8").splitlines(),
            ["♩=95", '= 117 "Water"'],
        )

    def test_survives_a_renamed_hook_point(self) -> None:
        """homr переименовал функцию — остаёмся без темпа, но не падаем."""
        from omr.engines._homr_runner import _install_tempo_capture

        del self.stub.is_tempo_marking
        _install_tempo_capture(str(self.image))
        self.assertFalse(self.image.with_suffix(".ocr.txt").exists())


class RecognizeAggregationTest(unittest.TestCase):
    def test_collects_page_texts_in_order_without_duplicates(self) -> None:
        """Отметка стоит в начале пьесы, поэтому порядок страниц важен. Повторы
        (шапка, продублированная на каждой странице) в разбор попадать не должны."""
        from omr.recognize import PageResult, RecognizeResult

        pages = [
            PageResult(number=1, source=Path("a.png"), ocr_texts=["J= 95", "3"]),
            PageResult(number=2, source=Path("a.png"), ocr_texts=["3", "= 72"]),
        ]
        result = RecognizeResult(musicxml=None, pages=pages)

        self.assertEqual(result.ocr_texts, ["J= 95", "3", "= 72"])
        self.assertEqual(bpm_from_texts(result.ocr_texts), 95)


class EngineSidecarTest(unittest.TestCase):
    def test_reads_lines_and_survives_a_missing_file(self) -> None:
        from omr.engines.homr_engine import _read_ocr_sidecar

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.ocr.txt"
            self.assertEqual(_read_ocr_sidecar(path), [])
            path.write_text("♩=95\n\n  3  \n", encoding="utf-8")
            self.assertEqual(_read_ocr_sidecar(path), ["♩=95", "3"])


TWO_PARTS = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>P1</part-name></score-part>
    <score-part id="P2"><part-name>P2</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1"><attributes><divisions>1</divisions></attributes>
      <note><rest/><duration>4</duration></note></measure>
    <measure number="2">
      <direction><direction-type><metronome><beat-unit>quarter</beat-unit>
        <per-minute>95</per-minute></metronome></direction-type><sound tempo="95"/></direction>
      <note><rest/><duration>4</duration></note></measure>
  </part>
  <part id="P2">
    <measure number="1"><attributes><divisions>1</divisions></attributes>
      <direction placement="above"><direction-type><metronome><beat-unit>half</beat-unit>
        <per-minute>70</per-minute></metronome></direction-type><sound tempo="140"/></direction>
      <note><rest/><duration>4</duration></note></measure>
    <measure number="2"><note><rest/><duration>4</duration></note></measure>
  </part>
</score-partwise>
"""


def _tempo_marks(path: Path) -> list[tuple[str, str, str | None]]:
    """(партия, такт, sound tempo) для каждой отметки темпа."""
    from api.analysis import _read_musicxml_root

    marks = []
    for part in _read_musicxml_root(path).iter("part"):
        for measure in part.iter("measure"):
            for direction in measure.iter("direction"):
                sound = direction.find("sound")
                if direction.find(".//metronome") is not None or sound is not None:
                    marks.append((part.get("id"), measure.get("number"),
                                  sound.get("tempo") if sound is not None else None))
    return marks


class ReplaceStartTempoTest(unittest.TestCase):
    """Темп ударных — наш OCR: отметка начала заменяется, смены темпа дальше остаются."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.score = Path(self._tmp.name) / "page.musicxml"
        self.score.write_text(TWO_PARTS)

    def test_start_metronome_is_the_printed_number(self) -> None:
        """«𝅗𝅥 = 70»: напечатано 70, а `<sound tempo>` уже пересчитан в четверти."""
        from api.analysis import start_metronome

        self.assertEqual(start_metronome(self.score), 70)
        self.assertEqual(collect_bpm(self.score), 95)   # первый в документе — такт 2 у P1

    def test_replaces_the_start_tempo_of_every_part_and_keeps_later_changes(self) -> None:
        from api.analysis import replace_bpm

        self.assertTrue(replace_bpm(self.score, 87))

        self.assertEqual(collect_bpm(self.score), 87)
        self.assertEqual(_tempo_marks(self.score), [("P1", "1", "87"), ("P1", "2", "95")])

    def test_works_inside_mxl(self) -> None:
        import zipfile

        from api.analysis import replace_bpm

        packed = self.score.with_suffix(".mxl")
        with zipfile.ZipFile(packed, "w") as archive:
            archive.writestr("META-INF/container.xml",
                             '<container><rootfiles><rootfile full-path="score.xml"/>'
                             "</rootfiles></container>")
            archive.writestr("score.xml", TWO_PARTS)

        self.assertTrue(replace_bpm(packed, 87))
        self.assertEqual(collect_bpm(packed), 87)


def _drum_page(staff_top: int, width: int = 1400, interline: int = 20, tilt: int = 0):
    """Страница с двумя пятилинейными станами, первый — с `staff_top`.

    `tilt` — на сколько px правый конец линий выше левого (наклонное фото).
    """
    import cv2
    import numpy as np

    image = np.full((staff_top + 16 * interline, width), 255, np.uint8)
    for top in (staff_top, staff_top + 9 * interline):
        for k in range(5):
            y = top + k * interline
            cv2.line(image, (60, y), (width - 60, y - tilt), 0, 2)
        for i in range(14):
            x = 120 + i * 85
            cy = top + (i % 5) * interline // 2 - tilt * (x - 60) // (width - 120)
            cv2.ellipse(image, (x, cy), (13, 10), -20, 0, 360, 0, -1)
            cv2.line(image, (x + 12, cy), (x + 12, cy - 70), 0, 2)
    return image


class TempoOcrTest(unittest.TestCase):
    """api/tempo_ocr.py: строки над первым станом, слева направо, первая страница с нотами.

    Сам RapidOCR подменён: здесь проверяется, ЧТО ему дают и что делают с ответом.
    Строки задаются в координатах страницы; подмена переводит их в масштаб картинки,
    которую получила (страницу перед OCR приводят к интервалу 24 px).
    """

    WIDTH = 1400

    def setUp(self) -> None:
        try:
            import cv2  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("нужен OpenCV")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.shown: list[tuple[float, float]] = []   # (высота, ширина) в координатах страницы

    def _reader(self, *passes: list[tuple[float, float, str]]):
        """Ответ OCR на каждую картинку запуска по очереди (масштаб 24, 28); последний повторяется."""
        from unittest import mock

        import numpy as np

        from api import tempo_ocr

        def batch(images):
            answers = []
            for index, image in enumerate(images):
                scale = image.shape[1] / self.WIDTH
                self.shown.append((image.shape[0] / scale, image.shape[1] / scale))
                lines = passes[min(index, len(passes) - 1)]
                answers.append([(np.float32([[x, y], [x + 120, y], [x + 120, y + 30], [x, y + 30]]) * scale,
                                 text) for x, y, text in lines])
            return answers

        return mock.patch.object(tempo_ocr, "_ocr_batch", batch)

    def _png(self, name: str, image) -> Path:
        import cv2

        path = self.dir / name
        cv2.imwrite(str(path), image)
        return path

    def test_reads_the_mark_above_the_first_staff(self) -> None:
        from api.tempo_ocr import read_tempo

        page = self._png("page.png", _drum_page(staff_top=300))
        with self._reader([(500, 100, "WONDERWALL - Oasis"), (40, 40, "J= 87")]):
            reading = read_tempo([page])

        self.assertEqual((reading.bpm, reading.page), (87, 0))
        self.assertEqual(reading.texts, ["J= 87", "WONDERWALL - Oasis"])
        height, width = self.shown[0]
        self.assertEqual(round(width), self.WIDTH)     # вся ширина: отметка бывает в углу
        self.assertTrue(380 <= height < 480, height)   # первый стан целиком, второго нет

    def test_text_inside_the_staff_is_not_the_mark(self) -> None:
        """Цифры размера, триоли, «X·X» на нотах — внутри стана; отметка — над ним."""
        from api.tempo_ocr import read_tempo

        page = self._png("page.png", _drum_page(staff_top=300))
        with self._reader([(40, 320, "J=60"), (40, 230, "J=90")]):
            self.assertEqual(read_tempo([page]).bpm, 90)

    def test_tilted_staff_is_compared_where_the_mark_stands(self) -> None:
        """Фото: правый конец стана на 60 px выше левого. Отметка над левым концом —
        ниже правого; срез по одной высоте отрезал бы её (Pull Me Under)."""
        from api.tempo_ocr import read_tempo

        page = self._png("page.png", _drum_page(staff_top=360, tilt=60))
        with self._reader([(60, 290, "J= 102")]):
            self.assertEqual(read_tempo([page]).bpm, 102)

    def test_the_leftmost_of_several_marks_is_the_start(self) -> None:
        """Venus: над первым станом 90 (такт 1) и 95 (такт 2); OCR отдаёт 95 первой — она выше."""
        from api.tempo_ocr import read_tempo

        page = self._png("page.png", _drum_page(staff_top=300))
        with self._reader([(700, 200, "=95"), (300, 240, "J=90")]):
            self.assertEqual(read_tempo([page]).bpm, 90)

    def test_second_scale_is_used_when_the_first_reads_nothing(self) -> None:
        """Pull Me Under: на одном масштабе OCR теряет «=» и читает голое «102»."""
        from api import tempo_ocr

        page = self._png("page.png", _drum_page(staff_top=300))
        with self._reader([(40, 240, "102")], [(40, 240, "J= 102")]):
            reading = tempo_ocr.read_tempo([page])

        self.assertEqual(reading.bpm, 102)
        self.assertEqual(len(self.shown), len(tempo_ocr.OCR_INTERLINES))

    def test_cover_page_is_skipped(self) -> None:
        import numpy as np

        from api.tempo_ocr import read_tempo

        cover = self._png("0.png", np.full((1800, self.WIDTH), 255, np.uint8))
        page = self._png("1.png", _drum_page(staff_top=300))
        with self._reader([(40, 240, "J=120")]):
            reading = read_tempo([cover, page])

        self.assertEqual((reading.bpm, reading.page), (120, 1))
        self.assertEqual(len(self.shown), 2)   # обложку в OCR не отдавали: два масштаба страницы 2

    def test_mark_on_a_later_page_is_not_the_start_tempo(self) -> None:
        """Нет отметки на первой странице с нотами — дальше не ищем: там смена темпа."""
        from api import tempo_ocr

        first = self._png("0.png", _drum_page(staff_top=300))
        second = self._png("1.png", _drum_page(staff_top=300))
        with self._reader([(40, 240, "Moderate rock")]):
            reading = tempo_ocr.read_tempo([first, second])

        self.assertIsNone(reading.bpm)
        self.assertEqual(reading.page, 0)
        self.assertEqual(reading.texts, ["Moderate rock"])
        self.assertEqual(len(self.shown), len(tempo_ocr.OCR_INTERLINES))   # одна страница


STUB_ONNXRUNTIME = """
class RapidOCR:
    def __call__(self, path):
        box = [[10, 20], [130, 20], [130, 50], [10, 50]]
        return [[box, "J= 87:" + path.rsplit("/", 1)[-1], 0.9]], [0.1]
"""

STUB_RAPIDOCR = """
class _Output:
    def __init__(self, path):
        self.boxes = [[[10.0, 20.0], [130.0, 20.0], [130.0, 50.0], [10.0, 50.0]]]
        self.txts = ("J= 87:" + str(path).rsplit("/", 1)[-1],)

class RapidOCR:
    def __call__(self, path):
        return _Output(path)
"""


class TempoOcrRunnerTest(unittest.TestCase):
    """api/tempo_ocr_runner.py: отдельный процесс, все картинки за один запуск, JSON в файл.

    RapidOCR подменён модулем-заглушкой: проверяется контракт раннера, а не OCR.
    """

    def setUp(self) -> None:
        try:
            import cv2  # noqa: F401
            import numpy as np
        except ImportError:  # pragma: no cover
            self.skipTest("нужен OpenCV")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.images = [np.full((60, 200), 255, np.uint8), np.full((60, 200), 255, np.uint8)]

    def _stub(self, onnxruntime: str | None, rapidocr: str | None):
        from unittest import mock

        for name, body in (("rapidocr_onnxruntime", onnxruntime), ("rapidocr", rapidocr)):
            if body is not None:
                (self.dir / name).mkdir()
                (self.dir / name / "__init__.py").write_text(body)
        return mock.patch.dict("os.environ", {"PYTHONPATH": str(self.dir), "OMR_HOMR_PYTHON": sys.executable})

    def test_reads_every_image_in_one_run(self) -> None:
        from api import tempo_ocr

        with self._stub(STUB_ONNXRUNTIME, None):
            pages = tempo_ocr._ocr_batch(self.images)

        self.assertEqual([[text for _, text in lines] for lines in pages], [["J= 87:0.png"], ["J= 87:1.png"]])
        self.assertEqual(pages[0][0][0].shape, (4, 2))

    def test_falls_back_to_rapidocr_of_newer_homr(self) -> None:
        from api import tempo_ocr

        with self._stub("raise ImportError('нет')", STUB_RAPIDOCR):
            pages = tempo_ocr._ocr_batch(self.images[:1])

        self.assertEqual([text for _, text in pages[0]], ["J= 87:0.png"])

    def test_failure_is_an_ocr_error(self) -> None:
        from api import tempo_ocr

        with self._stub("raise ImportError('No module named onnxruntime')", "raise ImportError('нет')"):
            with self.assertRaises(tempo_ocr.OcrError) as caught:
                tempo_ocr._ocr_batch(self.images[:1])

        self.assertIn("ImportError", str(caught.exception))


def _metronome_mark(hollow: bool):
    """«♩ = 70» или «𝅗𝅥 = 70»: головка со штилем вверх, знак равенства, число."""
    import cv2
    import numpy as np

    image = np.full((120, 300), 255, np.uint8)
    cv2.ellipse(image, (45, 80), (11, 8), -25, 0, 360, 0, 3 if hollow else -1)
    cv2.line(image, (55, 77), (55, 22), 0, 3)
    cv2.putText(image, "= 70", (70, 88), cv2.FONT_HERSHEY_SIMPLEX, 1.4, 0, 4)
    return image


class HalfNoteTest(unittest.TestCase):
    """Little Drummer Boy: «𝅗𝅥 = 70» — это 140 четвертей; OCR читает ноту буквой «J»."""

    def setUp(self) -> None:
        try:
            import cv2  # noqa: F401
            import numpy as np
        except ImportError:  # pragma: no cover
            self.skipTest("нужен OpenCV")
        # Рамка OCR — с «=» и далее: ноту она часто не включает.
        self.box = np.float32([[62, 40], [200, 40], [200, 95], [62, 95]])

    def test_hollow_head_is_a_half_note(self) -> None:
        from api.tempo_ocr import half_note

        self.assertTrue(half_note(_metronome_mark(hollow=True), self.box))

    def test_filled_head_is_a_quarter(self) -> None:
        from api.tempo_ocr import half_note

        self.assertFalse(half_note(_metronome_mark(hollow=False), self.box))

    def test_digit_with_a_hole_is_not_a_note(self) -> None:
        """В рамке только «70»: левый знак — цифра, и дырка в «0» тут ни при чём."""
        import numpy as np

        from api.tempo_ocr import half_note

        digits_only = np.float32([[150, 40], [200, 40], [200, 95], [150, 95]])
        self.assertFalse(half_note(_metronome_mark(hollow=True), digits_only))

    def test_reading_counts_quarters(self) -> None:
        from api.tempo_ocr import TempoReading

        self.assertEqual(TempoReading(printed=70, unit="half", page=0).bpm, 140)
        self.assertEqual(TempoReading(printed=70, page=0).bpm, 70)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
