"""Тесты защитных правок MusicXML и гейта verovio.

Каждый тест сторожит конкретную ошибку, которая уже была допущена или могла
проехать незамеченной, — а не просто «функция что-то вернула».
"""

from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from api.analysis import (
    _declare_staves,
    _drop_chord_beams,
    _percussion_snapshot,
    _reduced_copy,
    _restore_percussion,
    _sanitize_clefs,
    _scrub_root,
    _sanitize_divisions,
    repair,
    salvage,
)
from api.verovio_check import renders_ok


def part_xml(part_id: str, measures: list[tuple[str, int]]) -> str:
    """Партия из тактов (divisions, длительность каждой из четырёх нот)."""
    body = []
    for number, (divisions, duration) in enumerate(measures, start=1):
        attributes = (
            f"<attributes><divisions>{divisions}</divisions>"
            "<key><fifths>0</fifths></key>"
            "<time><beats>4</beats><beat-type>4</beat-type></time>"
            "<clef><sign>G</sign><line>2</line></clef></attributes>"
        )
        notes = "".join(
            "<note><pitch><step>C</step><octave>4</octave></pitch>"
            f"<duration>{duration}</duration><type>quarter</type></note>"
            for _ in range(4)
        )
        body.append(f'<measure number="{number}">{attributes}{notes}</measure>')
    return f'<part id="{part_id}">{"".join(body)}</part>'


def score_xml(*parts: str) -> str:
    listing = "".join(
        f'<score-part id="P{i}"><part-name>P{i}</part-name></score-part>'
        for i in range(1, len(parts) + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<score-partwise version="4.0">'
        f"<part-list>{listing}</part-list>{''.join(parts)}</score-partwise>"
    )


def divisions_of(root) -> list[str]:
    return [(d.text or "").strip() for d in root.iter("divisions")]


class SanitizeDivisionsTest(unittest.TestCase):
    """<divisions>0 роняет music21 (ZeroDivisionError), но чинить его константой нельзя."""

    def test_broken_part_takes_value_from_a_sibling_part(self) -> None:
        """Главный тест. Константа 480 при соседних 4 растянула бы партию в 120 раз.

        Файл при этом остаётся валидным и просто играет мусор — поэтому ошибку
        видно только так: сравнением с соседней партией.
        """
        root = ET.fromstring(score_xml(part_xml("P1", [("0", 4)]), part_xml("P2", [("4", 4)])))
        _sanitize_divisions(root)
        self.assertEqual(divisions_of(root), ["4", "4"])

    def test_broken_measure_takes_the_prevailing_value_of_its_own_part(self) -> None:
        """Своя партия ближе, чем чужая: даже если у соседа значение другое."""
        root = ET.fromstring(
            score_xml(
                part_xml("P1", [("8", 8), ("0", 8), ("8", 8)]),
                part_xml("P2", [("4", 4)]),
            )
        )
        _sanitize_divisions(root)
        self.assertEqual(divisions_of(root), ["8", "8", "8", "4"])

    def test_falls_back_to_constant_only_when_nothing_valid_exists(self) -> None:
        root = ET.fromstring(score_xml(part_xml("P1", [("0", 4)])))
        _sanitize_divisions(root)
        self.assertEqual(divisions_of(root), ["480"])

    def test_non_numeric_divisions_are_repaired_too(self) -> None:
        root = ET.fromstring(score_xml(part_xml("P1", [("", 4)]), part_xml("P2", [("12", 4)])))
        _sanitize_divisions(root)
        self.assertEqual(divisions_of(root), ["12", "12"])

    def test_healthy_file_is_left_alone(self) -> None:
        """Партия вправе менять divisions между тактами — трогать это нельзя."""
        root = ET.fromstring(score_xml(part_xml("P1", [("4", 4), ("8", 8)])))
        _sanitize_divisions(root)
        self.assertEqual(divisions_of(root), ["4", "8"])

    def test_durations_stay_in_sync_between_parts(self) -> None:
        """Сквозная проверка через music21 — ровно тот эффект, ради которого всё это."""
        from music21 import converter

        root = ET.fromstring(score_xml(part_xml("P1", [("0", 4)]), part_xml("P2", [("4", 4)])))
        _sanitize_divisions(root)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "score.musicxml"
            ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
            score = converter.parse(str(path))
            lengths = [
                {float(n.duration.quarterLength) for n in part.recurse().notes}
                for part in score.parts
            ]
        self.assertEqual(lengths[0], lengths[1], "партии разъехались по длительностям")


class SanitizeClefsTest(unittest.TestCase):
    def test_out_of_range_line_is_clamped_to_something_music21_accepts(self) -> None:
        """Кламп бесполезен, если music21 всё равно не примет результат."""
        from music21 import clef

        root = ET.fromstring(score_xml(part_xml("P1", [("4", 4)])))
        for line in root.iter("line"):
            line.text = "6"
        _sanitize_clefs(root)
        values = [(line.text or "") for line in root.iter("line")]
        self.assertEqual(values, ["5"])
        clef.clefFromString("G5")  # не должно кинуть ClefException

    def test_valid_lines_are_untouched(self) -> None:
        root = ET.fromstring(score_xml(part_xml("P1", [("4", 4)])))
        _sanitize_clefs(root)
        self.assertEqual([(line.text or "") for line in root.iter("line")], ["2"])


def drum_measure(member_beams: bool, head_beams: bool = True) -> str:
    """Такт ударных как у Audiveris: четыре аккорда «бочка/малый + хай-хэт» восьмыми."""
    def note(step: str, octave: int, chord: bool, beam: str | None) -> str:
        return (
            "<note>" + ("<chord/>" if chord else "")
            + f"<unpitched><display-step>{step}</display-step>"
            f"<display-octave>{octave}</display-octave></unpitched>"
            "<duration>1</duration><voice>1</voice><type>eighth</type><stem>up</stem>"
            + ("<notehead>x</notehead>" if chord else "")
            + (f'<beam number="1">{beam}</beam>' if beam else "")
            + "</note>"
        )
    body = []
    for index, (step, octave) in enumerate([("F", 4), ("C", 5), ("F", 4), ("C", 5)]):
        state = ("begin", "continue", "continue", "end")[index]
        body.append(note(step, octave, False, state if head_beams else None))
        body.append(note("G", 5, True, state if member_beams else None))
    return (
        '<part id="P1"><measure number="1"><attributes><divisions>2</divisions>'
        "<time><beats>2</beats><beat-type>4</beat-type></time>"
        "<clef><sign>percussion</sign></clef></attributes>"
        + "".join(body) + "</measure></part>"
    )


def beams_by_role(root) -> tuple[int, int]:
    notes = list(root.iter("note"))
    heads = sum(len(n.findall("beam")) for n in notes if n.find("chord") is None)
    members = sum(len(n.findall("beam")) for n in notes if n.find("chord") is not None)
    return heads, members


class DropChordBeamsTest(unittest.TestCase):
    """<beam> на ноте-члене аккорда: verovio не падает, а молча теряет ноты."""

    def test_members_lose_beams_heads_keep_them(self) -> None:
        root = ET.fromstring(score_xml(drum_measure(member_beams=True)))
        _drop_chord_beams(root)
        self.assertEqual(beams_by_role(root), (4, 0))

    def test_member_keeps_beam_when_the_head_has_none(self) -> None:
        """Иначе группировка восьмых пропала бы совсем."""
        root = ET.fromstring(score_xml(drum_measure(member_beams=True, head_beams=False)))
        _drop_chord_beams(root)
        self.assertEqual(beams_by_role(root), (0, 4))

    def test_verovio_sees_every_note_after_the_fix(self) -> None:
        """Сквозная проверка — ровно тот эффект, ради которого правка: до неё
        verovio выбрасывал ноты аккордов (на эталоне ударных — половину)."""
        import re

        import verovio

        def seen(root) -> int:
            toolkit = verovio.toolkit()
            toolkit.loadData(ET.tostring(root, encoding="unicode"))
            return len(re.findall(r"<note\b", toolkit.getMEI()))

        root = ET.fromstring(score_xml(drum_measure(member_beams=True)))
        before = seen(root)
        _drop_chord_beams(root)
        self.assertEqual(seen(root), 8)
        self.assertLess(before, 8, "без правки verovio должен терять ноты — иначе тест ничего не сторожит")


class LabelPercussionPartsTest(unittest.TestCase):
    """Имена партий обнуляем, а MuseScore по пустому имени берёт фортепиано —
    и играет им ударные (жалоба с прода на metal-drum)."""

    def test_drum_part_is_named_drumset_and_others_stay_blank(self) -> None:
        drums = drum_measure(member_beams=False).replace('<part id="P1">', '<part id="P2">')
        root = ET.fromstring(score_xml(part_xml("P1", [("4", 4)]), drums))
        _scrub_root(root)
        names = {sp.get("id"): sp.findtext("part-name") for sp in root.iter("score-part")}
        self.assertEqual(names, {"P1": "", "P2": "Drumset"})

    def test_percussion_clef_alone_is_enough(self) -> None:
        """Такт ударных без нот (одни паузы) — партия всё равно ударная."""
        rests = (
            '<part id="P1"><measure number="1"><attributes><divisions>1</divisions>'
            "<clef><sign>percussion</sign></clef></attributes>"
            "<note><rest/><duration>4</duration><type>whole</type></note></measure></part>"
        )
        root = ET.fromstring(score_xml(rests))
        _scrub_root(root)
        self.assertEqual([sp.findtext("part-name") for sp in root.iter("score-part")], ["Drumset"])


# Установка как у Audiveris: бочка F4, малый C5, хай-хэт G5 крестом.
DRUM_KIT = {
    ("F", "4", ""): ("P1-I36", 36, "Bass_Drum_1"),
    ("C", "5", ""): ("P1-I38", 38, "Acoustic_Snare"),
    ("G", "5", "x"): ("P1-I42", 42, "Closed_Hi_Hat"),
}


def drum_kit_score() -> str:
    definitions = "".join(
        f'<score-instrument id="{ident}"><instrument-name>{name}</instrument-name></score-instrument>'
        for ident, _, name in DRUM_KIT.values()
    ) + "".join(
        f'<midi-instrument id="{ident}"><midi-channel>10</midi-channel>'
        f"<midi-program>1</midi-program><midi-unpitched>{key}</midi-unpitched></midi-instrument>"
        for ident, key, _ in DRUM_KIT.values()
    )

    def note(step: str, octave: str, head: str, chord: bool) -> str:
        ident = DRUM_KIT[(step, octave, head)][0]
        return (
            "<note>" + ("<chord/>" if chord else "")
            + f"<unpitched><display-step>{step}</display-step>"
            f"<display-octave>{octave}</display-octave></unpitched>"
            f'<duration>1</duration><instrument id="{ident}"/><voice>1</voice>'
            "<type>quarter</type><stem>up</stem>"
            + (f"<notehead>{head}</notehead>" if head else "") + "</note>"
        )

    notes = "".join(
        note(step, octave, "", False) + note("G", "5", "x", True)
        for step, octave in [("F", "4"), ("C", "5"), ("F", "4"), ("C", "5")]
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><score-partwise version="4.0"><part-list>'
        f'<score-part id="P1"><part-name>Drs.</part-name>{definitions}</score-part></part-list>'
        '<part id="P1"><measure number="1"><attributes><divisions>1</divisions>'
        "<time><beats>4</beats><beat-type>4</beat-type></time>"
        f"<clef><sign>percussion</sign></clef></attributes>{notes}</measure></part></score-partwise>"
    )


def sounds_of(root) -> list[tuple[tuple[str, str, str], int | None]]:
    """(место, головка) каждой ноты -> MIDI-звук, который она реально получит."""
    unpitched = {
        mi.get("id"): int(mi.findtext("midi-unpitched"))
        for mi in root.iter("midi-instrument") if mi.findtext("midi-unpitched")
    }
    result = []
    for note in root.iter("note"):
        if note.find("unpitched") is None:
            continue
        key = (note.findtext("unpitched/display-step"), note.findtext("unpitched/display-octave"),
               (note.findtext("notehead") or "").strip())
        instrument = note.find("instrument")
        result.append((key, unpitched.get(instrument.get("id")) if instrument is not None else None))
    return result


class RepairKeepsDrumsTest(unittest.TestCase):
    """music21 (repair) схлопывал установку в один звук — «заявлено барабаны,
    а играет не то». Инструменты должны пережить repair."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "drums.musicxml"
        self.path.write_text(drum_kit_score(), encoding="utf-8")

    def test_music21_alone_loses_the_kit(self) -> None:
        """Иначе тест ниже ничего не сторожит."""
        from music21 import converter

        written = Path(self._tmp.name) / "plain.musicxml"
        converter.parse(str(self.path)).write("musicxml", fp=str(written))
        heard = {key for _, key in sounds_of(ET.parse(written).getroot())}
        self.assertNotEqual(heard, {36, 38, 42}, "music21 перестал ломать установку — правка не нужна?")

    def test_every_hit_keeps_its_own_sound_after_repair(self) -> None:
        fixed = repair(self.path)
        self.assertIsNotNone(fixed)
        root = ET.parse(fixed).getroot()
        sounds = sounds_of(root)
        self.assertEqual(len(sounds), 8)
        for key, sound in sounds:
            self.assertEqual(sound, DRUM_KIT[key][1], f"{key} звучит не тем инструментом")
        self.assertEqual([sp.findtext("part-name") for sp in root.iter("score-part")], ["Drumset"])

    def test_restore_refuses_when_a_hit_has_no_known_sound(self) -> None:
        """Нота на месте, которого в исходнике не было, — лучше отказаться
        (repair вернёт None, сработает salvage), чем отдать установку с дырами."""
        original = ET.fromstring(drum_kit_score())
        snapshot = _percussion_snapshot(original)
        broken = ET.fromstring(drum_kit_score())
        broken.find(".//unpitched/display-step").text = "A"
        broken.find(".//unpitched/display-octave").text = "3"
        self.assertFalse(_restore_percussion(broken, snapshot))

    def test_score_without_drums_takes_no_snapshot(self) -> None:
        root = ET.fromstring(score_xml(part_xml("P1", [("4", 4)])))
        self.assertEqual(_percussion_snapshot(root), {})


class DeclareStavesTest(unittest.TestCase):
    """homr пишет у нот `<staff>2</staff>`, но не объявляет `<staves>2</staves>`.

    Без объявления verovio падал на загрузке, а music21 в `repair` сливал руки
    фортепиано в один стан — клиенту уходил файл без гранд-стана.
    """

    GRAND_STAFF = (
        '<score-partwise><part-list><score-part id="P1"><part-name/></score-part>'
        '</part-list><part id="P1"><measure number="1">'
        "<attributes><divisions>1</divisions><key><fifths>0</fifths></key>"
        "<time><beats>4</beats><beat-type>4</beat-type></time>"
        '<clef number="1"><sign>G</sign><line>2</line></clef>'
        '<clef number="2"><sign>F</sign><line>4</line></clef></attributes>'
        "<note><pitch><step>E</step><octave>5</octave></pitch><duration>4</duration>"
        "<voice>1</voice><type>whole</type><staff>1</staff></note>"
        "<backup><duration>4</duration></backup>"
        "<note><pitch><step>C</step><octave>3</octave></pitch><duration>4</duration>"
        "<voice>5</voice><type>whole</type><staff>2</staff></note>"
        "</measure></part></score-partwise>"
    )

    def test_declares_the_second_staff_in_schema_order(self) -> None:
        root = ET.fromstring(self.GRAND_STAFF)
        _declare_staves(root)
        attributes = root.find(".//attributes")
        self.assertEqual(attributes.findtext("staves"), "2")
        order = [child.tag for child in attributes]
        self.assertLess(order.index("time"), order.index("staves"))
        self.assertLess(order.index("staves"), order.index("clef"))

    def test_music21_keeps_both_hands_after_the_fix(self) -> None:
        """Ровно то, что делает `repair`: без объявления станов музыка сливается."""
        from music21 import converter

        root = ET.fromstring(self.GRAND_STAFF)
        _declare_staves(root)
        score = converter.parseData(ET.tostring(root, encoding="unicode"), format="musicxml")
        self.assertEqual(len(score.parts), 2)   # music21 раскладывает гранд-стан на два PartStaff

    def test_single_staff_and_already_declared_parts_are_left_alone(self) -> None:
        single = ET.fromstring(score_xml(part_xml("P1", [("1", 1)])))
        _declare_staves(single)
        self.assertIsNone(single.find(".//staves"))

        declared = ET.fromstring(self.GRAND_STAFF.replace(
            "<clef number=\"1\">", "<staves>2</staves><clef number=\"1\">"))
        _declare_staves(declared)
        self.assertEqual(len(declared.findall(".//staves")), 1)


class SalvageTest(unittest.TestCase):
    """Последняя ступень: выбросить проблемный кусок, чтобы уцелело остальное.

    Проверка `accepts` подставляется фейковой — так тестируется САМ алгоритм
    поиска, а не капризы verovio.
    """

    def score_of(self, measures: int, parts: int = 1) -> str:
        return score_xml(*[
            part_xml(f"P{p}", [("4", 4)] * measures) for p in range(1, parts + 1)
        ])

    def write(self, text: str, directory: str, name: str = "score.musicxml") -> Path:
        path = Path(directory) / name
        path.write_text(text)
        return path

    @staticmethod
    def rejects_measure(index: int):
        """Фейковая проверка: файл годен, только если в нём не осталось такта `index`.

        Такты помечены длительностью, чтобы их можно было опознать после вырезания.
        """
        def accepts(path: Path) -> bool:
            root = ET.parse(path).getroot()
            part = root.find("part")
            if part is None:
                return False
            numbers = [m.get("number") for m in part.findall("measure")]
            return str(index + 1) not in numbers
        return accepts

    def test_drops_exactly_the_one_bad_measure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(self.score_of(16), tmp)
            result = salvage(path, self.rejects_measure(9))
            self.assertIsNotNone(result)
            out, report = result
            self.assertEqual(report["dropped_measures"], 1)
            self.assertEqual(report["total_measures"], 16)
            numbers = [m.get("number") for m in ET.parse(out).getroot().find("part").findall("measure")]
            self.assertNotIn("10", numbers)
            self.assertEqual(len(numbers), 15)

    def test_finds_the_bad_measure_wherever_it_is(self) -> None:
        """Бисекция обязана работать и на краях, а не только в середине."""
        for bad in (0, 1, 7, 14, 15):
            with tempfile.TemporaryDirectory() as tmp:
                path = self.write(self.score_of(16), tmp)
                result = salvage(path, self.rejects_measure(bad))
                self.assertIsNotNone(result, f"такт {bad} не найден")
                out, report = result
                self.assertEqual(report["dropped_measures"], 1, f"такт {bad}: лишнее выброшено")

    def test_gives_up_instead_of_returning_a_stump(self) -> None:
        """Обрубок хуже честного провала: бюджет на выброшенное ограничен."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(self.score_of(10), tmp)
            self.assertIsNone(salvage(path, lambda p: False))

    def test_returns_none_when_the_file_is_already_fine(self) -> None:
        """Ничего не выброшено — значит и спасать было нечего."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(self.score_of(8), tmp)
            self.assertIsNone(salvage(path, lambda p: True))

    def test_prefers_dropping_a_whole_broken_part(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(self.score_of(8, parts=2), tmp)

            def accepts(p: Path) -> bool:
                root = ET.parse(p).getroot()
                return root.find(".//part[@id='P2']") is None

            result = salvage(path, accepts)
            self.assertIsNotNone(result)
            out, report = result
            self.assertEqual(report["dropped_parts"], 1)
            self.assertEqual(report["dropped_measures"], 0)
            root = ET.parse(out).getroot()
            # Объявление партии в <part-list> обязано уйти вместе с самой партией.
            self.assertIsNone(root.find(".//score-part[@id='P2']"))

    def test_leaves_no_probe_files_behind(self) -> None:
        """Промежуточные файлы лежали бы в раздаваемой директории /media."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(self.score_of(12), tmp)
            salvage(path, self.rejects_measure(5))
            leftovers = [p.name for p in Path(tmp).glob("*probe*")]
            self.assertEqual(leftovers, [])


class ReducedCopyTest(unittest.TestCase):
    def test_carries_all_attribute_blocks_forward(self) -> None:
        """Блоков <attributes> в такте бывает несколько.

        Сторожит реальную ошибку: переносился только первый, и вместе с
        выброшенным тактом у остатка пропадал ключ — движок такое не рисует.
        """
        root = ET.fromstring(score_xml(part_xml("P1", [("4", 4), ("4", 4)])))
        first = root.find("part").findall("measure")[0]
        extra = ET.SubElement(first, "attributes")
        ET.SubElement(extra, "staves").text = "2"

        reduced = _reduced_copy(root, set(), {0})
        survivor = reduced.find("part").findall("measure")[0]
        carried = {child.tag for block in survivor.findall("attributes") for child in block}
        self.assertIn("staves", carried)
        self.assertIn("clef", carried)
        self.assertIn("divisions", carried)

    def test_drops_the_measure_from_every_part_at_once(self) -> None:
        """Иначе партии разъедутся по времени — файл станет хуже, чем был."""
        root = ET.fromstring(score_xml(
            part_xml("P1", [("4", 4)] * 5), part_xml("P2", [("4", 4)] * 5)))
        reduced = _reduced_copy(root, set(), {2})
        counts = [len(p.findall("measure")) for p in reduced.findall("part")]
        self.assertEqual(counts, [4, 4])

    def test_can_skip_carrying_attributes(self) -> None:
        """Второй режим нужен: бывают файлы, которые движок принимает только без переноса."""
        root = ET.fromstring(score_xml(part_xml("P1", [("4", 4), ("4", 4)])))
        first = root.find("part").findall("measure")[0]
        marker = ET.SubElement(first, "attributes")
        ET.SubElement(marker, "staves").text = "2"

        reduced = _reduced_copy(root, set(), {0}, carry_attributes=False)
        survivor = reduced.find("part").findall("measure")[0]
        carried = {child.tag for block in survivor.findall("attributes") for child in block}
        self.assertNotIn("staves", carried)   # ничего не унесли из выброшенного такта
        self.assertIn("clef", carried)        # собственные атрибуты такта на месте


class RendersOkTest(unittest.TestCase):
    """Гейт повторяет то, что делает мобильное приложение: загрузка + MIDI + вёрстка."""

    def test_accepts_a_healthy_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.musicxml"
            path.write_text(score_xml(part_xml("P1", [("4", 4)])))
            self.assertTrue(renders_ok(path))

    def test_rejects_a_file_that_is_not_musicxml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "junk.musicxml"
            path.write_text("не музыка, а просто текст")
            self.assertFalse(renders_ok(path))

    def test_survives_a_missing_file_instead_of_raising(self) -> None:
        """Гейт обязан деградировать в False: он стоит на пути живой задачи."""
        self.assertFalse(renders_ok(Path("/nonexistent/score.musicxml")))


if __name__ == "__main__":
    unittest.main()
