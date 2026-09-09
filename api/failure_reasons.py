"""Причины провалов OMR: словарь + классификатор текста ошибки.

Зачем. В архиве провалов (`failed_files`) лежал только текст ошибки, и по нему
нельзя было ни понять картину, ни посчитать что-либо: сто строк разного текста
про одно и то же. Здесь тексты сводятся к десятку причин, по которым уже можно
считать метрику и решать, что чинить.

Причины подобраны по ДЕЙСТВИЮ, а не по месту в коде: «движок упал» — это к нам,
«на входе не партитура» — это к пользователю, «не уложились в таймаут» — это к
ресурсам. Две ошибки из разных модулей с одинаковым действием живут в одной
причине.

Классификация идёт по тексту, а не по типу исключения, потому что до архива
доезжает только текст (воркер собирает строки из `FileResult.error`). Тексты все
наши собственные, поэтому сопоставление надёжно — и каждое сообщение, которое
код умеет выдать, закреплено тестом в tests/test_failure_reasons.py. Если текст
изменят и забудут про тест, запись получит `unknown`: в админке видно сразу,
потому что причина «не определена» вынесена в общий список.
"""

from __future__ import annotations

import re
from enum import Enum


class FailureReason(str, Enum):
    """Причина провала. Значение хранится в БД, менять существующие нельзя."""

    NO_MUSIC = "no_music"
    BAD_SCALE = "bad_scale"
    ENGINE_FAILED = "engine_failed"
    TIMEOUT = "timeout"
    UNREADABLE_INPUT = "unreadable_input"
    UNSUPPORTED_FORMAT = "unsupported_format"
    INVALID_MUSICXML = "invalid_musicxml"
    MERGE_FAILED = "merge_failed"
    INTERNAL_ERROR = "internal_error"
    UNKNOWN = "unknown"


#: Подпись в админке и подсказка, что с этим делать.
LABELS: dict[str, tuple[str, str]] = {
    FailureReason.NO_MUSIC: (
        "Нот не найдено",
        "На входе не партитура (скриншот, обложка, фото не того) либо нотные "
        "станы неразличимы. Работа с пользователем и подсказками при загрузке.",
    ),
    FailureReason.BAD_SCALE: (
        "Стан вне рабочего масштаба",
        "Кадр слишком крупный (крупный план одного стана) или слишком мелкий. "
        "Движку нужна страница целиком; подсказка при съёмке решает это лучше "
        "любой обработки.",
    ),
    FailureReason.ENGINE_FAILED: (
        "Движок не справился",
        "homr упал или не отдал MusicXML на странице, где ноты есть. Это к нам: "
        "смотреть лог движка и файл в архиве.",
    ),
    FailureReason.TIMEOUT: (
        "Таймаут",
        "Не уложились в отведённое время. Обычно очень большой файл или "
        "многостраничный PDF — вопрос лимитов и ресурсов, а не распознавания.",
    ),
    FailureReason.UNREADABLE_INPUT: (
        "Файл не прочитан",
        "Битый файл, нечитаемый формат или отсутствующий кодек (HEIC без "
        "pillow-heif). До распознавания дело не дошло.",
    ),
    FailureReason.UNSUPPORTED_FORMAT: (
        "Формат не поддержан",
        "Ни один движок не берётся за такой файл. Либо расширять приём, либо "
        "отсекать на клиенте.",
    ),
    FailureReason.INVALID_MUSICXML: (
        "MusicXML не принят verovio",
        "Ноты распознались, но результат не проходит проверку рендером — и его "
        "не спасли ни music21-фикс, ни выброс проблемных тактов. Это к нам.",
    ),
    FailureReason.MERGE_FAILED: (
        "Склейка страниц",
        "Страницы распознались, но не собрались в одну партитуру. Это к нам: "
        "смотреть relieur.",
    ),
    FailureReason.INTERNAL_ERROR: (
        "Внутренний сбой",
        "Необработанное исключение в пайплайне или воркере. Всегда баг.",
    ),
    FailureReason.UNKNOWN: (
        "Причина не определена",
        "Текст ошибки не подошёл ни под одно правило: либо запись сделана до "
        "введения причин, либо сообщение изменили и не обновили классификатор.",
    ),
}

# Порядок важен: сообщение бывает составным («страниц без нот; движок упал»),
# и тогда выигрывает первое совпавшее правило. Поэтому сначала идут причины «это
# к нам» — падение движка мы хотим видеть даже вперемешку с пустыми страницами.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (FailureReason.TIMEOUT, re.compile(
        r"таймаут|timed out|TIMEOUT", re.I)),
    (FailureReason.INTERNAL_ERROR, re.compile(
        r"упал[аи]?\b|Traceback|Error:|Exception", re.I)),
    (FailureReason.MERGE_FAILED, re.compile(
        r"не удалось склеить", re.I)),
    (FailureReason.INVALID_MUSICXML, re.compile(
        r"verovio|валидный MusicXML", re.I)),
    # Масштаб стана — корневая причина, а падение движка — её следствие, поэтому
    # правило стоит раньше: интервал вне рабочего коридора значит, что движок на
    # этом кадре не заработает, сколько его ни перезапускай.
    (FailureReason.BAD_SCALE, re.compile(
        r"стан слишком (крупный|мелкий)", re.I)),
    # Именно «homr не дал»: префикс моста «Пайплайн omr не дал MusicXML: …» есть
    # у ЛЮБОГО провала пайплайна, и по нему причину не отличить.
    (FailureReason.ENGINE_FAILED, re.compile(
        r"homr не дал MusicXML|не смог распознать|нет MusicXML на выходе", re.I)),
    (FailureReason.NO_MUSIC, re.compile(
        r"станов не найдено|не нашло музыки|нет страниц с нотами", re.I)),
    (FailureReason.UNREADABLE_INPUT, re.compile(
        r"не могу прочитать|файла нет|не удалось прочитать", re.I)),
    (FailureReason.UNSUPPORTED_FORMAT, re.compile(
        r"не берёт ни один движок", re.I)),
)


def classify(error: str | None) -> str:
    """Свести текст ошибки к причине. Пустой текст — тоже `unknown`."""
    if not error:
        return FailureReason.UNKNOWN.value
    for reason, pattern in _RULES:
        if pattern.search(error):
            return reason.value
    return FailureReason.UNKNOWN.value


def label(reason: str | None) -> str:
    """Подпись причины для админки."""
    entry = LABELS.get(reason or FailureReason.UNKNOWN.value)
    return entry[0] if entry else (reason or "")


def hint(reason: str | None) -> str:
    """Что эта причина означает и к кому она."""
    entry = LABELS.get(reason or FailureReason.UNKNOWN.value)
    return entry[1] if entry else ""
