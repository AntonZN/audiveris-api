"""Тесты символов: нормализация лиг/связок (`omr.notation`) и перенос ремарок
из Audiveris в выход homr (`omr.transplant`).

Каждый тест сторожит решение, принятое по замеру на `tests/images/symbols`, —
а не просто «функция что-то вернула».
"""

from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from omr.notation import normalize
from omr.symcheck import compare_symbols
from omr.transplant import transplant

HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n'


def note(step: str, octave: int, duration: int = 4, *, chord: bool = False, staff: int = 1,
         slur: str | None = None, extra: str = "") -> str:
    notations = f'<notations><slur type="{slur}"/></notations>' if slur else ""
    return (
        "<note>" + ("<chord/>" if chord else "")
        + f"<pitch><step>{step}</step><octave>{octave}</octave></pitch>"
        + f"<duration>{duration}</duration><voice>1</voice><type>quarter</type>"
        + f"<staff>{staff}</staff>{notations}{extra}</note>"
    )


def score(*parts: list[str], measure_prefix: str = "") -> str:
    """Партитура: каждая партия — список тел тактов (4/4, divisions=4)."""
    part_list = "".join(f'<score-part id="P{i + 1}"><part-name/></score-part>'
                        for i in range(len(parts)))
    body = []
    for index, measures in enumerate(parts):
        xml = []
        for number, content in enumerate(measures, start=1):
            attributes = ("<attributes><divisions>4</divisions><time><beats>4</beats>"
                          "<beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line>"
                          "</clef></attributes>") if number == 1 else ""
            xml.append(f'<measure number="{number}">{attributes}{content}</measure>')
        body.append(f'<part id="P{index + 1}">{"".join(xml)}</part>')
    return (HEADER + f'<score-partwise version="4.0"><part-list>{part_list}</part-list>'
            + "".join(body) + "</score-partwise>")


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path


class NotationTests(Workspace):
    def test_slur_between_same_pitches_becomes_sounding_tie(self) -> None:
        # Модель homr связок не выдаёт — она рисует их лигой. Без правки такая
        # «лига» звучит двумя ударами.
        path = self.write("tie.musicxml", score([
            note("C", 5, slur="start") + note("C", 5, slur="stop") + note("D", 5) + note("E", 5),
        ]))
        report = normalize(path)
        root = ET.parse(path).getroot()
        notes = root.findall(".//note")
        self.assertEqual(report.counts["лиг стало связками"], 1)
        self.assertEqual(len(root.findall(".//slur")), 0)
        self.assertEqual([t.get("type") for t in notes[0].findall("tie")], ["start"])
        self.assertEqual([t.get("type") for t in notes[1].findall("tie")], ["stop"])
        self.assertIsNotNone(notes[0].find("notations/tied[@type='start']"))
        # <tie> стоит сразу после <duration>: там его ждёт схема.
        tags = [child.tag for child in notes[0]]
        self.assertEqual(tags.index("tie"), tags.index("duration") + 1)

    def test_phrase_slur_between_chords_with_common_tone_stays_slur(self) -> None:
        # Правило «общий тон» на Debussy превращало фразовые лиги в ложные связки.
        path = self.write("chords.musicxml", score([
            note("C", 5, slur="start") + note("E", 5, chord=True)
            + note("C", 5, slur="stop") + note("F", 5, chord=True)
            + note("D", 5) + note("E", 5),
        ]))
        normalize(path)
        root = ET.parse(path).getroot()
        self.assertEqual(len(root.findall(".//tie")), 0)
        self.assertEqual(len(root.findall(".//slur")), 2)

    def test_orphans_removed_and_overlapping_slurs_numbered(self) -> None:
        path = self.write("orphans.musicxml", score([
            note("C", 5, slur="stop") + note("D", 5, slur="start")
            + note("E", 5, slur="start") + note("F", 5, slur="stop"),
            note("G", 5, slur="stop") + note("A", 5, slur="start") + note("B", 5) + note("C", 6),
        ]))
        report = normalize(path)
        root = ET.parse(path).getroot()
        slurs = [(s.get("type"), s.get("number")) for s in root.iter("slur")]
        self.assertEqual(report.counts["лиг без начала удалено"], 1)
        self.assertEqual(report.counts["лиг без конца удалено"], 1)
        # LIFO: F закрывает E, G — D; две лиги перекрываются и получают разные номера.
        self.assertEqual(sorted(slurs), [("start", "1"), ("start", "2"), ("stop", "1"), ("stop", "2")])

    def test_forward_repeat_moves_to_left_barline(self) -> None:
        # homr 0.6.2 вешает repeatStart на правую черту — повтор уезжал на такт позже.
        barline = '<barline location="right"><repeat direction="forward"/></barline>'
        path = self.write("repeat.musicxml", score([
            note("C", 5, 16),
            note("D", 5, 16) + barline,
        ]))
        normalize(path)
        measure = ET.parse(path).getroot().findall(".//measure")[1]
        self.assertEqual(measure[0].tag, "barline")
        self.assertEqual(measure[0].get("location"), "left")
        self.assertEqual(measure[0].find("repeat").get("direction"), "forward")
        self.assertEqual(len(measure.findall("barline")), 1)


def direction(content: str, staff: int | None = None, sound: str = "") -> str:
    staff_xml = f"<staff>{staff}</staff>" if staff else ""
    return (f'<direction placement="below"><direction-type>{content}</direction-type>'
            f"{staff_xml}{sound}</direction>")


MELODY = [("C", 5), ("D", 5), ("E", 5), ("F", 5)]
MELODY2 = [("G", 5), ("A", 5), ("B", 5), ("C", 6)]


def bar(pitches, before: dict[int, str] | None = None) -> str:
    before = before or {}
    return "".join(before.get(i, "") + note(step, octave) for i, (step, octave) in enumerate(pitches))


class TransplantTests(Workspace):
    def test_dynamics_lands_before_its_note_with_sound_and_without_layout(self) -> None:
        homr = self.write("homr.musicxml", score([bar(MELODY), bar(MELODY2)]))
        dyn = direction('<dynamics default-x="12" default-y="-40"><p/></dynamics>',
                        sound='<sound dynamics="54"/>')
        audiveris = self.write("aud.musicxml", score([bar(MELODY, {2: dyn}), bar(MELODY2)]))
        report = transplant(homr, audiveris)
        self.assertEqual(report.added["dynamics"], 1)
        measure = ET.parse(homr).getroot().findall(".//measure")[0]
        children = list(measure)
        placed = measure.find("direction")
        following = children[children.index(placed) + 1]
        self.assertEqual(following.findtext("pitch/step"), "E")
        self.assertIsNone(placed.find(".//dynamics").get("default-y"))
        self.assertEqual(placed.findtext("staff"), "1")
        self.assertEqual(placed.find("sound").get("dynamics"), "54")

    def test_wedge_without_stop_is_not_transplanted(self) -> None:
        # Непарная вилка — та же беда, что непарная лига для verovio в мобиле.
        homr = self.write("homr.musicxml", score([bar(MELODY), bar(MELODY2)]))
        start = direction('<wedge type="crescendo" number="1"/>')
        audiveris = self.write("aud.musicxml", score([bar(MELODY, {1: start}), bar(MELODY2)]))
        report = transplant(homr, audiveris)
        self.assertFalse(report.changed)
        self.assertEqual(len(list(ET.parse(homr).getroot().iter("wedge"))), 0)

    def test_wedge_pair_crosses_barline(self) -> None:
        homr = self.write("homr.musicxml", score([bar(MELODY), bar(MELODY2)]))
        start = direction('<wedge type="crescendo" number="1"/>')
        stop = direction('<wedge type="stop" number="1"/>')
        audiveris = self.write("aud.musicxml", score([bar(MELODY, {2: start}),
                                                      bar(MELODY2, {1: stop})]))
        report = transplant(homr, audiveris)
        self.assertEqual(report.added["wedge"], 1)
        measures = ET.parse(homr).getroot().findall(".//measure")
        self.assertEqual(measures[0].find(".//wedge").get("type"), "crescendo")
        self.assertEqual(measures[1].find(".//wedge").get("type"), "stop")

    def test_octave_shift_moves_covered_notes_to_sounding_pitch(self) -> None:
        # Оба движка пишут под 8va написанную высоту, MusicXML хочет звучащую.
        homr = self.write("homr.musicxml", score([bar(MELODY), bar(MELODY2)]))
        start = direction('<octave-shift type="down" size="8" number="1"/>', staff=1)
        stop = direction('<octave-shift type="stop" size="8" number="1"/>', staff=1)
        audiveris = self.write("aud.musicxml", score([bar(MELODY, {1: start, 3: stop}),
                                                      bar(MELODY2)]))
        report = transplant(homr, audiveris)
        self.assertEqual(report.added["octave-shift"], 1)
        octaves = [int(n.findtext("pitch/octave")) for n in ET.parse(homr).getroot().iter("note")]
        # Под линией D и E (C до неё, F после конца линии не звучит сдвинутым).
        self.assertEqual(octaves[:4], [5, 6, 6, 5])
        self.assertEqual(octaves[4:], [5, 5, 5, 6])

    def test_repeat_goes_to_all_parts_only_by_majority(self) -> None:
        backward = '<barline location="right"><repeat direction="backward"/></barline>'
        other = [("A", 4), ("B", 4), ("C", 5), ("D", 5)]
        other2 = [("E", 4), ("F", 4), ("G", 4), ("A", 4)]
        homr = self.write("homr.musicxml", score([bar(MELODY), bar(MELODY2)],
                                                 [bar(other), bar(other2)]))
        # Повтор только у одной партии из двух — половина, не большинство... а
        # половина считается достаточной? Нет: нужна хотя бы половина, и одна из
        # двух — это половина. Поэтому берём три партии и повтор у одной.
        third = [("C", 4), ("E", 4), ("G", 4), ("C", 5)]
        third2 = [("D", 4), ("F", 4), ("A", 4), ("D", 5)]
        homr = self.write("homr.musicxml", score([bar(MELODY), bar(MELODY2)],
                                                 [bar(other), bar(other2)],
                                                 [bar(third), bar(third2)]))
        lonely = self.write("aud1.musicxml", score([bar(MELODY) + backward, bar(MELODY2)],
                                                   [bar(other), bar(other2)],
                                                   [bar(third), bar(third2)]))
        report = transplant(homr, lonely)
        self.assertEqual(report.added["repeat"], 0)
        self.assertEqual(len(list(ET.parse(homr).getroot().iter("repeat"))), 0)

        agreed = self.write("aud2.musicxml", score([bar(MELODY) + backward, bar(MELODY2)],
                                                   [bar(other) + backward, bar(other2)],
                                                   [bar(third), bar(third2)]))
        report = transplant(homr, agreed)
        self.assertEqual(report.added["repeat"], 1)
        root = ET.parse(homr).getroot()
        for part in root.findall("part"):
            first = part.findall("measure")[0]
            self.assertEqual(first.find("barline/repeat").get("direction"), "backward")


class TransplantGuardTests(Workspace):
    def test_second_transplant_does_not_duplicate(self) -> None:
        # Audiveris отдаёт страницу несколькими частями, а в режиме both — ещё и с
        # двух картинок: двойная динамика хуже, чем ничего.
        homr = self.write("homr.musicxml", score([bar(MELODY), bar(MELODY2)]))
        dyn = direction("<dynamics><f/></dynamics>")
        start = direction('<wedge type="crescendo" number="1"/>')
        stop = direction('<wedge type="stop" number="1"/>')
        audiveris = self.write("aud.musicxml", score([bar(MELODY, {0: dyn, 1: start}),
                                                      bar(MELODY2, {1: stop})]))
        first = transplant(homr, audiveris)
        second = transplant(homr, audiveris)
        self.assertEqual((first.added["dynamics"], first.added["wedge"]), (1, 1))
        self.assertFalse(second.changed)
        root = ET.parse(homr).getroot()
        self.assertEqual(len(list(root.iter("dynamics"))), 1)
        self.assertEqual(len(list(root.iter("wedge"))), 2)


class SymbolsImageTests(unittest.TestCase):
    def test_both_gives_raster_first_and_photo_only_prepared(self) -> None:
        from omr.recognize import PageResult, _symbols_images

        pdf_page = PageResult(number=1, source=Path("page01.png"),
                              engine_image=Path("p.clean.png"), raster_image=Path("page01.png"))
        photo = PageResult(number=1, source=Path("photo.heic"), engine_image=Path("photo.clean.png"))
        self.assertEqual(_symbols_images(pdf_page, "both"), [Path("page01.png"), Path("p.clean.png")])
        self.assertEqual(_symbols_images(pdf_page, "raster"), [Path("page01.png")])
        self.assertEqual(_symbols_images(pdf_page, "prepared"), [Path("p.clean.png")])
        # У фото растра нет, HEIC пользователя Audiveris не читает — только кадр.
        self.assertEqual(_symbols_images(photo, "both"), [Path("photo.clean.png")])
        self.assertEqual(_symbols_images(photo, "raster"), [Path("photo.clean.png")])


class SymcheckTests(Workspace):
    def test_symbols_outside_the_reference_are_not_false_positives(self) -> None:
        # Эталон — одна партия (виолончель), выход — ещё и фортепиано с динамикой:
        # это не ложные срабатывания, эталон просто про другое.
        dyn = direction("<dynamics><f/></dynamics>")
        other = [("A", 3), ("B", 3), ("C", 4), ("D", 4)]
        reference = self.write("ref.musicxml", score([bar(MELODY, {0: dyn}), bar(MELODY2)]))
        output = self.write("out.musicxml", score([bar(MELODY, {0: dyn}), bar(MELODY2)],
                                                  [bar(other, {0: dyn}), bar(other, {1: dyn})]))
        result = compare_symbols(output, reference).scores["dynamics"]
        self.assertEqual((result.reference, result.candidate, result.matched), (1, 1, 1))

    def test_reference_against_itself_is_perfect(self) -> None:
        dyn = direction("<dynamics><f/></dynamics>")
        text = score([bar(MELODY, {0: dyn}),
                      note("C", 5, slur="start") + note("D", 5) + note("E", 5)
                      + note("F", 5, slur="stop")])
        path = self.write("ref.musicxml", text)
        comparison = compare_symbols(path, path)
        for kind in ("dynamics", "slur"):
            score_ = comparison.scores[kind]
            self.assertEqual((score_.matched, score_.true_candidate), (1, 1), kind)


if __name__ == "__main__":
    unittest.main()
