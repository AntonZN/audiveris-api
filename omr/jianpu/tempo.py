"""Темп цзянпу: метроном, слово по учебной шкале, 散板 — без темпа.

Где темп на листе. В начале пьесы, слева сверху: сразу после «1=C 3/4» или строкой
под ними. Пишут метрономом (`♩=72`) или словом — итальянским термином, его
китайским названием (中板 — Moderato) или простым 中速 «умеренно»; рядом часто
характер («中速 深情地»). 散板 (знак «サ») — свободный ритм, темпа нет.

Что читает движок. jpeditor берёт из заголовка только метроном. Строку со словом
он выбрасывает, а набранную крупно — делает названием («中速深情地» вместо
«梦里故乡»). Все строки заголовка перехватывает раннер (`_jianpu_runner.mjs`),
темп и настоящее название достаются из них здесь.

Слово -> BPM. Единой шкалы нет: у 行板 в учебных таблицах 66, в китайской Википедии
76–108. Берём одну учебную шкалу — одно число на термин — и в отчёте прямо пишем,
что темп оценён по слову. У простых слов (慢速, 稍慢, 中速, 稍快, 快速) чисел в
источниках нет, они приравнены к ближайшему по смыслу термину шкалы.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from omr.engines.jianpu_engine import HeaderLine

# Учебная шкала: одно число на термин (источники — omr/jianpu/README.md).
TEXTBOOK_BPM = {
    "广板": 46, "Largo": 46,
    "慢板": 52, "Lento": 52,
    "柔板": 56, "Adagio": 56,
    "行板": 66, "Andante": 66,
    "小行板": 69, "Andantino": 69,
    "中板": 88, "Moderato": 88,
    "小快板": 108, "Allegretto": 108,
    "快板": 132, "Allegro": 132,
    "急板": 184, "Presto": 184,
}
# Простые слова — к ближайшему по смыслу термину шкалы.
PLAIN_WORDS = {"慢速": "慢板", "稍慢": "行板", "中速": "中板", "稍快": "小快板", "快速": "快板"}
# Длинные первыми: «小快板» не должен читаться как «快板».
_TERMS = sorted({*TEXTBOOK_BPM, *PLAIN_WORDS}, key=len, reverse=True)
_FREE = re.compile(r"散板|散拍|速度自由|自由地|[サ艹]")
# Число только после «=»: «1=C» и номер страницы темпом не станут. ♩ OCR теряет или
# читает как J — на него не опираемся.
_METRONOME = re.compile(r"[=＝]\s*(\d{2,3})(?!\d)")
# Не название: тональность, размер, «作词：…», «…词/曲/谱», «转C调».
_NOT_TITLE = re.compile(r"[1１6６]\s*[=＝]|^\s*[\d/\s]+$|[作詞词曲編编譯译]\s*[:：]|[詞词曲谱譜]\s*$|指法|转.{0,3}调")
_HANZI = re.compile(r"[㐀-鿿]")


@dataclass
class Tempo:
    bpm: int | None
    how: str           # для отчёта
    words: str = ""    # слово темпа, как на листе; пусто — метроном


def term_in(text: str) -> str | None:
    """Термин темпа в строке: самый левый, при равенстве — самый длинный."""
    best: tuple[int, str] | None = None
    for term in _TERMS:
        if term.isascii():
            match = re.search(rf"(?<![A-Za-z]){term}(?![A-Za-z])", text, re.I)
        else:
            match = re.search(re.escape(term), text)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), term)
    return best[1] if best else None


def is_tempo_text(text: str) -> bool:
    return bool(term_in(text) or _METRONOME.search(text) or _FREE.search(text))


def from_header(texts: list[str]) -> Tempo | None:
    """Темп из строк заголовка; None — на листе его нет.

    Метроном важнее слова: число написано явно, а слово даёт только оценку. 散板
    важнее слова: свободный ритм с темпом из шкалы сыграли бы неверно.
    """
    for text in texts:
        match = _METRONOME.search(text)
        if match and 30 <= int(match.group(1)) <= 300:
            return Tempo(int(match.group(1)), f"{match.group(1)} — метроном «{text}»")
    for text in texts:
        if _FREE.search(text):
            return Tempo(None, f"«{text}» — свободный ритм (散板), темп не ставим")
    for text in texts:
        term = term_in(text)
        if term:
            base = PLAIN_WORDS.get(term, term)
            bpm = TEXTBOOK_BPM[base]
            via = f" как {base}" if base != term else ""
            return Tempo(bpm, f"{bpm} — оценка по слову «{term}»{via}, учебная шкала", words=term)
    return None


def _same(a: str, b: str) -> bool:
    return re.sub(r"\s", "", a) == re.sub(r"\s", "", b)


def true_title(current: str, lines: list[HeaderLine]) -> str:
    """Настоящее название, если движок сделал названием строку темпа; иначе пусто.

    Заменяем, только если в заголовке есть строка-название не ниже захватчика: у
    пьесы «如歌的行板» 行板 — часть названия, и другой такой строки там нет.
    """
    if not current or not is_tempo_text(current):
        return ""
    taken = next((line for line in lines if _same(line.text, current)), None)
    candidates = [line for line in lines
                  if len(_HANZI.findall(line.text)) >= 2 and not _NOT_TITLE.search(line.text)
                  and not is_tempo_text(line.text)
                  and (taken is None or line.height >= 0.9 * taken.height)]
    return max(candidates, key=lambda line: line.height).text.strip() if candidates else ""


def apply(musicxml: Path, lines: list[HeaderLine]) -> list[str]:
    """Название и темп из строк заголовка — в MusicXML. Возвращает заметки для отчёта."""
    tree = ET.parse(musicxml)
    root = tree.getroot()
    notes: list[str] = []
    changed = False

    title_element = root.find("work/work-title")
    title = (title_element.text or "").strip() if title_element is not None else ""
    replacement = true_title(title, lines)
    if replacement:
        notes.append(f"название: «{title}» — это строка темпа, взято «{replacement}» из заголовка")
        title_element.text = title = replacement
        changed = True

    if root.find(".//sound[@tempo]") is not None or root.find(".//per-minute") is not None:
        notes.append("темп: метроном прочитан движком")
    else:
        # Строку названия темпом не читаем: «如歌的行板» — пьеса, а не указание темпа.
        found = from_header([line.text for line in lines if not _same(line.text, title)])
        if found is None:
            notes.append("темп: на листе не указан")
        else:
            notes.append(f"темп: {found.how}")
            if found.bpm is not None:
                _insert_tempo(root, found)
                changed = True
    if changed:
        tree.write(musicxml, encoding="utf-8", xml_declaration=True)
    return notes


def _insert_tempo(root: ET.Element, tempo: Tempo) -> None:
    """`<direction>` с `<sound tempo>` в начало первого такта первой партии.

    Слово пишется как есть (`<words>中速</words>`), метроном — `<metronome>`; число
    для плеера в обоих случаях в `<sound tempo>`, его и читает `collect_bpm` в API.
    """
    measure = root.find("part/measure")
    if measure is None:
        return
    direction = ET.Element("direction", placement="above")
    kind = ET.SubElement(direction, "direction-type")
    if tempo.words:
        ET.SubElement(kind, "words").text = tempo.words
    else:
        metronome = ET.SubElement(kind, "metronome")
        ET.SubElement(metronome, "beat-unit").text = "quarter"
        ET.SubElement(metronome, "per-minute").text = str(tempo.bpm)
    ET.SubElement(direction, "sound", tempo=str(tempo.bpm))
    # После служебного начала такта (разрыв системы, атрибуты, левая черта с вольтой).
    position = len(measure)
    for index, child in enumerate(measure):
        if child.tag not in ("print", "attributes", "barline"):
            position = index
            break
    measure.insert(position, direction)
