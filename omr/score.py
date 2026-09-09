"""Метрика качества распознавания: насколько выход похож на эталон.

Считать «сколько нот нашлось» бесполезно: движок легко выдаёт ровно столько же
нот, но не тех. Поэтому сравниваем ПОСЛЕДОВАТЕЛЬНОСТИ (высота, длительность) —
расстоянием Левенштейна, нормированным на длину эталона. Это ровно тот показатель,
которым в OMR принято мерить точность, и он чувствителен и к пропускам, и к
подменам, и к лишним нотам.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Sequence:
    notes: list[tuple]
    measures: int
    parts: int

    def __len__(self) -> int:
        return len(self.notes)


def read(path: Path) -> Sequence:
    """Достать из MusicXML последовательность нот в порядке чтения."""
    root = ET.parse(Path(path)).getroot()
    notes: list[tuple] = []
    for note in root.iter("note"):
        if note.find("rest") is not None:
            notes.append(("rest", note.findtext("type") or ""))
            continue
        pitch = note.find("pitch")
        if pitch is None:
            continue
        notes.append((
            pitch.findtext("step") or "",
            pitch.findtext("octave") or "",
            pitch.findtext("alter") or "0",
            note.findtext("type") or "",
        ))
    return Sequence(
        notes,
        measures=len(list(root.iter("measure"))),
        parts=len(list(root.iter("part"))),
    )


def accuracy(candidate: Sequence, reference: Sequence) -> float:
    """Доля эталона, воспроизведённая верно: 1 - расстояние/длина. Может быть < 0."""
    if not reference.notes:
        return 0.0
    distance = levenshtein(candidate.notes, reference.notes)
    return 1.0 - distance / len(reference.notes)


def levenshtein(a: list, b: list) -> int:
    """Расстояние редактирования между двумя списками (две строки таблицы, O(min) памяти)."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, item_a in enumerate(a, start=1):
        current = [i]
        for j, item_b in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,                              # удаление
                current[j - 1] + 1,                           # вставка
                previous[j - 1] + (item_a != item_b),         # замена
            ))
        previous = current
    return previous[-1]
