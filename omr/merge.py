"""Склейка постраничных MusicXML в одну партитуру.

Нужна ровно потому, что PDF и «плейлист» — это одна песня, разложенная по
страницам, а движок видит каждую страницу отдельно и не знает о соседях.

Главная ловушка — РАЗНОЕ ЧИСЛО ПАРТИЙ НА СТРАНИЦАХ. homr выводит партии из того,
что нашёл на конкретном листе: там, где на странице виден только верхний
нотоносец, партия будет одна, а на соседней — две. Склеить «партию 1 к партии 1»
и на этом успокоиться нельзя: партии разъедутся по длине, и дальше и music21, и
verovio получат рваную партитуру. Поэтому выравниваем на общее число партий, а
недостающие такты добиваем целотактовыми паузами.

Это намеренно ПРОСТАЯ склейка по индексу партии. В `api/relieur.py` живёт более
хитрая версия, которая сопоставляет партии по структурной сигнатуре; здесь она
не нужна — пакет `omr` самостоятелен и не тянет зависимостей из `api`.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_DIVISIONS = 4
_DEFAULT_BEATS = 4


@dataclass
class MergeReport:
    pages: int = 0
    parts: int = 0
    measures: int = 0
    padded_measures: int = 0
    dropped_parts: int = 0
    notes: list[str] = field(default_factory=list)


def merge(sources: list[Path], target: Path, *, max_padding: float = 0.5) -> MergeReport:
    """Склеить постраничные MusicXML в `target`. Возвращает отчёт о склейке.

    `max_padding` — доля пауз-заглушек, после которой партия признаётся
    призраком и выбрасывается. Смысл: партия, найденная движком на одной
    странице из четырёх, почти наверняка артефакт детекции, а не второй голос.
    Оставить её — значит отдать клиенту партитуру с инструментом, который
    молчит три четверти пьесы; на реальном `pdf-01` так и выходило: 66 тактов
    пауз из 76.
    """
    if not sources:
        raise ValueError("нечего склеивать: пустой список страниц")

    documents = [ET.parse(str(path)).getroot() for path in sources]
    report = MergeReport(pages=len(documents))

    part_count = max(len(document.findall("part")) for document in documents)
    report.parts = part_count

    merged = copy.deepcopy(documents[0])
    _ensure_parts(merged, part_count)

    targets = merged.findall("part")
    for part in targets:
        for measure in part.findall("measure"):
            part.remove(measure)

    padding = [0] * len(targets)
    for index, document in enumerate(documents):
        pages_parts = document.findall("part")
        # Сколько тактов на этой странице: берём максимум по её партиям, чтобы
        # короткая партия не обрезала страницу для остальных.
        page_measures = max((len(p.findall("measure")) for p in pages_parts), default=0)
        for slot, part in enumerate(targets):
            source = pages_parts[slot] if slot < len(pages_parts) else None
            measures = source.findall("measure") if source is not None else []
            for measure in measures:
                part.append(copy.deepcopy(measure))
            missing = page_measures - len(measures)
            if missing > 0:
                divisions, beats = _running_meter(part)
                for _ in range(missing):
                    part.append(_rest_measure(divisions, beats))
                padding[slot] += missing
                report.padded_measures += missing
                report.notes.append(
                    f"страница {index + 1}: партия {slot + 1} короче на {missing} тактов, "
                    "добита паузами"
                )

    _drop_ghost_parts(merged, targets, padding, max_padding, report)
    _renumber(merged)
    report.measures = max(
        (len(part.findall("measure")) for part in merged.findall("part")), default=0
    )
    ET.ElementTree(merged).write(str(target), encoding="utf-8", xml_declaration=True)
    return report


def _drop_ghost_parts(root, targets, padding, max_padding: float, report) -> None:
    """Выбросить партии, состоящие в основном из добитых пауз.

    Никогда не выбрасываем всё: если призрачными выглядят все партии, значит
    метрика врёт, и лучше отдать как есть.
    """
    for slot in range(len(targets) - 1, -1, -1):
        part = targets[slot]
        total = len(part.findall("measure"))
        if total == 0 or len(root.findall("part")) <= 1:
            continue
        if padding[slot] / total <= max_padding:
            continue
        identifier = part.get("id")
        root.remove(part)
        part_list = root.find("part-list")
        if part_list is not None:
            for declaration in part_list.findall("score-part"):
                if declaration.get("id") == identifier:
                    part_list.remove(declaration)
        report.dropped_parts += 1
        report.padded_measures -= padding[slot]
        report.notes.append(
            f"партия {slot + 1} выброшена: {padding[slot]} из {total} тактов — "
            "добитые паузы, это артефакт детекции, а не голос"
        )
    report.parts = len(root.findall("part"))


def _ensure_parts(root, count: int) -> None:
    """Довести число партий до `count`, клонируя объявление первой.

    Новая партия появляется, когда на какой-то странице движок увидел больше
    нотоносцев, чем на первой. Клонируем именно первую: её `<attributes>` —
    единственный источник ключа и размера, который у нас есть.
    """
    part_list = root.find("part-list")
    parts = root.findall("part")
    if not parts or part_list is None:
        return
    while len(parts) < count:
        number = len(parts) + 1
        identifier = f"P{number}"
        declaration = ET.SubElement(part_list, "score-part", {"id": identifier})
        ET.SubElement(declaration, "part-name").text = ""
        clone = copy.deepcopy(parts[0])
        clone.set("id", identifier)
        root.append(clone)
        parts = root.findall("part")


def _running_meter(part) -> tuple[int, int]:
    """Последние известные divisions и число долей в такте этой партии."""
    divisions, beats = _DEFAULT_DIVISIONS, _DEFAULT_BEATS
    for attributes in part.iter("attributes"):
        text = (attributes.findtext("divisions") or "").strip()
        if text.isdigit() and int(text) > 0:
            divisions = int(text)
        value = (attributes.findtext("time/beats") or "").strip()
        if value.isdigit() and int(value) > 0:
            beats = int(value)
    return divisions, beats


def _rest_measure(divisions: int, beats: int):
    """Такт из одной целотактовой паузы — стандартная заглушка MusicXML."""
    measure = ET.Element("measure")
    note = ET.SubElement(measure, "note")
    ET.SubElement(note, "rest", {"measure": "yes"})
    ET.SubElement(note, "duration").text = str(max(divisions * beats, 1))
    ET.SubElement(note, "voice").text = "1"
    return measure


def _renumber(root) -> None:
    """Сквозная нумерация тактов: после склейки номера страниц начинались с единицы."""
    for part in root.findall("part"):
        for number, measure in enumerate(part.findall("measure"), start=1):
            measure.set("number", str(number))
