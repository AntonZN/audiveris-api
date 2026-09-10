"""Тесты сверки `omr/refcheck.py` на ударных.

Эталоны `tests/images/drum` записаны нотами БЕЗ высоты (`<unpitched>`), а
refcheck раньше пропускал всё, где нет `<pitch>`, — эталон читался как 0 нот, и
любой выход «совпадал» на 0%. Тесты сторожат именно это.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omr import refcheck

_HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0"><part-list><score-part id="P1"><part-name>Drums</part-name>
</score-part></part-list><part id="P1"><measure number="1"><attributes><divisions>1</divisions>
<clef><sign>percussion</sign><line>2</line></clef></attributes>"""
_TAIL = "</measure></part></score-partwise>"


def _unpitched(step: str, octave: int, head: str = "") -> str:
    notehead = f"<notehead>{head}</notehead>" if head else ""
    return (f"<note><unpitched><display-step>{step}</display-step>"
            f"<display-octave>{octave}</display-octave></unpitched>"
            f"<duration>1</duration><type>quarter</type>{notehead}</note>")


def _pitched(step: str, octave: int) -> str:
    return (f"<note><pitch><step>{step}</step><octave>{octave}</octave></pitch>"
            f"<duration>1</duration><type>quarter</type></note>")


# Грув: бочка (F4), хай-хэт крестом (G5), малый (C5), хай-хэт крестом.
_GROOVE = [("F", 4, ""), ("G", 5, "x"), ("C", 5, ""), ("G", 5, "x")]


class RefcheckDrumTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _write(self, name: str, notes: str) -> Path:
        path = self.root / name
        path.write_text(_HEAD + notes + _TAIL, encoding="utf-8")
        return path

    def test_unpitched_notes_are_read_with_position_and_head(self) -> None:
        path = self._write("ref.musicxml", "".join(_unpitched(*n) for n in _GROOVE))
        notes = refcheck.read(path).notes
        self.assertEqual(len(notes), 4)
        self.assertTrue(all(n.unpitched for n in notes))
        self.assertEqual([n.head for n in notes], ["", "x", "", "x"])
        self.assertEqual(notes[0].degree, 4 * 7 + 3)   # F4

    def test_reference_against_itself_is_perfect(self) -> None:
        path = self._write("ref.musicxml", "".join(_unpitched(*n) for n in _GROOVE))
        result = refcheck.compare(path, path)
        self.assertEqual(result.pitch_f1, 1.0)
        self.assertEqual(result.strict, 1.0)
        self.assertEqual(result.position_f1, 1.0)
        self.assertEqual(result.unpitched, (4, 4))

    def test_wrong_head_is_a_miss_but_keeps_the_position(self) -> None:
        # Хай-хэт прочитан обычной головкой: место то же, инструмент другой.
        reference = self._write("ref.musicxml", "".join(_unpitched(*n) for n in _GROOVE))
        candidate = self._write("out.musicxml",
                                "".join(_unpitched(s, o) for s, o, _ in _GROOVE))
        result = refcheck.compare(candidate, reference)
        self.assertEqual(result.position_f1, 1.0)
        self.assertAlmostEqual(result.pitch_f1, 0.5)

    def test_pitched_output_compares_by_staff_position(self) -> None:
        # homr ударных не знает и пишет их нотами с высотой: место на стане
        # совпадает, головок нет — это и должно быть видно в цифрах.
        reference = self._write("ref.musicxml", "".join(_unpitched(*n) for n in _GROOVE))
        candidate = self._write("out.musicxml",
                                "".join(_pitched(s, o) for s, o, _ in _GROOVE))
        result = refcheck.compare(candidate, reference)
        self.assertEqual(result.unpitched, (0, 4))
        self.assertEqual(result.position_f1, 1.0)
        self.assertAlmostEqual(result.pitch_f1, 0.5)

    def test_pitched_reference_keeps_midi_comparison(self) -> None:
        # Без ударных всё по-старому: C4 и B#3 — одна высота.
        reference = self._write("ref.musicxml", _pitched("C", 4))
        candidate = self._write(
            "out.musicxml",
            "<note><pitch><step>B</step><alter>1</alter><octave>3</octave></pitch>"
            "<duration>1</duration><type>quarter</type></note>")
        result = refcheck.compare(candidate, reference)
        self.assertIsNone(result.position_f1)
        self.assertEqual(result.pitch_f1, 1.0)


if __name__ == "__main__":
    unittest.main()
