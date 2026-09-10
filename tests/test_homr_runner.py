"""Тесты правок раскладки станов в раннере homr (`omr/engines/_homr_runner.py`).

Сам homr здесь не нужен: функции раннера работают с его объектами только через
пару атрибутов (`staffs`, `min_y`, `size`…), поэтому проверяются на
заглушках. Геометрия заглушек взята из замеров на эталонах `tests/images`.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from omr.engines import _homr_runner as runner


class _Staff:
    def __init__(self, min_y, max_y, *, min_x=100.0, unit=13.0):
        self.min_y, self.max_y, self.min_x = float(min_y), float(max_y), float(min_x)
        self.average_unit_size = unit

    def merge(self, other):
        merged = _Staff(self.min_y, other.max_y, min_x=self.min_x, unit=self.average_unit_size)
        merged.pieces = (self, other)
        return merged


class _System:
    def __init__(self, staffs, connections=()):
        self.staffs = sorted(staffs, key=lambda staff: staff.min_y)
        self.connections = list(connections)


def _element(x, top, bottom, width):
    """«Высокий» элемент сегментации homr: акколада, скобка или черта."""
    return SimpleNamespace(center=(x, (top + bottom) / 2), size=(width, bottom - top))


def _cello_piano(top, *, gap_inside=60):
    """Система «виолончель + гранд-стан фортепиано» (гранд-стан уже склеен)."""
    cello = _Staff(top, top + 50)
    piano = _Staff(top + 50 + gap_inside, top + 50 + gap_inside + 250)
    return cello, piano


def _system_line(cello, piano):
    """Начальная черта системы: тонкая, через все её станы."""
    return _element(104, cello.min_y - 2, piano.max_y + 2, 6)


class SystemRejoinTest(unittest.TestCase):
    def test_split_system_is_glued_back(self) -> None:
        """Брамс, соч. 99, стр. 4: [2, 1, 1, 2, 2] — виолончель не связалась с
        фортепиано, и homr разложил страницу в одну партию (-24 такта у
        виолончели). Куски должны склеиться в обычную систему."""
        systems, top = [], 0
        for index in range(4):
            cello, piano = _cello_piano(top)
            if index == 1:
                systems += [_System([cello]), _System([piano])]
            else:
                systems.append(_System([cello, piano]))
            top += 360 + 150
        result = runner._rejoin_split_systems(systems, _System)
        self.assertEqual([len(system.staffs) for system in result], [2, 2, 2, 2])

    def test_system_line_decides_when_distance_misleads(self) -> None:
        """Брамс, стр. 1: промежуток «виолончель — фортепиано» ВНУТРИ системы
        (169 px) больше промежутка между системами (153 px). Одним расстоянием
        порванную систему не склеить — но через стык проходит черта системы."""
        systems, tall, top = [], [], 0
        for index in range(4):
            cello, piano = _cello_piano(top, gap_inside=169)
            tall.append(_system_line(cello, piano))
            if index == 2:
                systems += [_System([cello]), _System([piano])]
            else:
                systems.append(_System([cello, piano]))
            top += 50 + 169 + 250 + 153
        self.assertIsNone(runner._rejoin_split_systems(systems, _System))  # без черты — никак
        result = runner._rejoin_split_systems(systems, _System, tall)
        self.assertEqual([len(system.staffs) for system in result], [2, 2, 2, 2])

    def test_single_system_page_is_glued_by_its_bracket(self) -> None:
        """Корелли, стр. 1: одна система из семи станов распалась на 6 + 1.
        Целых соседних систем на странице нет, мерить расстояние не по чему, —
        но скобка системы накрывает все семь станов."""
        staffs = [_Staff(100 + 140 * i, 150 + 140 * i) for i in range(7)]
        bracket = _element(133, 95, 1000 + 55, 17)
        pieces = [_System(staffs[:6]), _System(staffs[6:])]
        self.assertIsNone(runner._rejoin_split_systems(pieces, _System))
        result = runner._rejoin_split_systems(pieces, _System, [bracket])
        self.assertEqual([len(system.staffs) for system in result], [7])

    def test_separate_system_is_not_glued(self) -> None:
        """Одиночная система далеко внизу, и черта её с соседями не связывает."""
        systems, tall, top = [], [], 0
        for index in range(3):
            cello, piano = _cello_piano(top)
            tall.append(_system_line(cello, piano))
            systems.append(_System([cello, piano]))
            top += 510
        lone = _Staff(top + 400, top + 450)
        tall.append(_element(104, lone.min_y - 2, lone.max_y + 2, 6))
        systems.append(_System([lone]))
        self.assertIsNone(runner._rejoin_split_systems(systems, _System, tall))

    def test_bar_line_inside_the_system_does_not_glue(self) -> None:
        """Элемент, накрывающий стык, но стоящий посреди строки, — не черта системы."""
        staffs = [_Staff(100 + 140 * i, 150 + 140 * i) for i in range(7)]
        inner = _element(900, 95, 1055, 6)
        pieces = [_System(staffs[:6]), _System(staffs[6:])]
        self.assertIsNone(runner._rejoin_split_systems(pieces, _System, [inner]))

    def test_uniform_page_is_left_alone(self) -> None:
        systems = [_System(list(_cello_piano(510 * i))) for i in range(3)]
        self.assertIsNone(runner._rejoin_split_systems(systems, _System))


class BracePairingTest(unittest.TestCase):
    def test_brace_pairs_piano_not_cello(self) -> None:
        """Брамс: тонкая черта системы накрывает все три стана, толстая акколада —
        только фортепиано. Гранд-стан — пара станов фортепиано, виолончель одна
        (в 0.7.0 длинная черта перебивала акколаду, и виолончель уезжала в
        правую руку)."""
        cello, upper, lower = _Staff(990, 1037), _Staff(1158, 1219), _Staff(1323, 1383)
        page = [cello, upper, lower]
        tall = [_element(124, 990, 1400, 6), _element(111, 1150, 1398, 16)]
        decision = runner._brace_decision(_System(page), tall, page)
        self.assertEqual(decision, {1})

    def test_bracket_group_means_separate_parts(self) -> None:
        """Корелли: скобки групп (0.7 интервала толщиной) на 3 и 4 стана, акколад
        нет. Каждый стан — отдельный инструмент, а не «четыре фортепиано»."""
        staffs = [_Staff(688 + 140 * i, 742 + 140 * i) for i in range(7)]
        tall = [_element(106, 680, 1060, 9), _element(106, 1100, 1650, 9)]
        self.assertEqual(runner._brace_decision(_System(staffs), tall, staffs), set())

    def test_brace_merged_with_system_line_is_not_a_bracket(self) -> None:
        """Lieder, превью 600 px: акколада фортепиано слилась с чертой системы в
        один толстый блок на «голос + фортепиано». Это не скобка ансамбля —
        иначе фортепиано разлетается на две партии. Решать нечем: пусть работает
        прежнее правило 0.6.2 (голос один, фортепиано парой)."""
        voice, upper, lower = _Staff(739, 805, unit=15), _Staff(887, 953, unit=15), _Staff(1038, 1102, unit=15)
        page = [voice, upper, lower]
        blob = _element(102, 735, 1110, 31)
        self.assertIsNone(runner._brace_decision(_System(page), [blob], page))

    def test_no_evidence_returns_none(self) -> None:
        """Нет ни акколады, ни скобки — решать нечем, остаётся поведение 0.6.2."""
        staffs = [_Staff(100, 160), _Staff(300, 360)]
        self.assertIsNone(runner._brace_decision(_System(staffs), [], staffs))

    def test_bar_line_inside_the_system_is_not_a_brace(self) -> None:
        """Черта посреди системы накрывает пару станов, но это не акколада."""
        staffs = [_Staff(100, 160), _Staff(300, 360)]
        tall = [_element(900, 95, 365, 16)]
        self.assertIsNone(runner._brace_decision(_System(staffs), tall, staffs))

    def test_pair_staffs_merges_only_chosen_pairs(self) -> None:
        staffs = [_Staff(100, 150), _Staff(200, 250), _Staff(300, 350)]
        result = runner._pair_staffs(_System(staffs), {1}, _System)
        self.assertEqual(len(result.staffs), 2)
        self.assertIs(result.staffs[0], staffs[0])
        self.assertEqual(result.staffs[1].pieces, (staffs[1], staffs[2]))


if __name__ == "__main__":
    unittest.main()
