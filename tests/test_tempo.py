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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
