"""Переводы названий справочников: {код языка: перевод}.

Хранятся JSON-колонкой рядом с сущностью и уходят в API как есть. Админка
редактирует их построчно («ru» → «Пианино»), а этот модуль превращает строки
формы в словарь и проверяет их — без зависимостей от БД и SQLAdmin.
"""

import re
from collections.abc import Iterable

# BCP 47 в упрощённом виде: язык (en, ru, fil) и необязательные подтеги
# (pt-BR, zh-Hans). Этого хватает для ключей локали на iOS/Android.
_LANG_RE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*")

MAX_VALUE_LENGTH = 120  # как у Instrument.name


def normalize_lang(raw: str) -> str:
    """`RU` → `ru`, `pt_BR` → `pt-BR`: язык в нижнем регистре, подтеги как есть."""
    head, *rest = raw.strip().replace("_", "-").split("-")
    return "-".join([head.lower(), *rest])


def parse_rows(rows: Iterable[tuple[str, str]]) -> tuple[dict[str, str], list[str]]:
    """Строки формы (язык, перевод) → словарь и список ошибок.

    Полностью пустые строки пропускаются: в форме всегда есть свободная строка
    под новый язык. Порядок языков сохраняется как ввели.
    """
    result: dict[str, str] = {}
    errors: list[str] = []
    for raw_lang, raw_value in rows:
        lang = normalize_lang(raw_lang or "")
        value = (raw_value or "").strip()
        if not lang and not value:
            continue
        if not lang:
            errors.append(f"Для перевода «{value}» не указан код языка")
            continue
        if not _LANG_RE.fullmatch(lang):
            errors.append(f"«{raw_lang.strip()}» — не код языка (пример: en, ru, pt-BR)")
            continue
        if not value:
            errors.append(f"Не заполнен перевод для «{lang}»")
            continue
        if len(value) > MAX_VALUE_LENGTH:
            errors.append(f"Перевод для «{lang}» длиннее {MAX_VALUE_LENGTH} символов")
            continue
        if lang in result:
            errors.append(f"Язык «{lang}» указан дважды")
            continue
        result[lang] = value
    return result, errors


def with_default_en(localization: dict | None, name: str | None) -> dict[str, str]:
    """Гарантировать ключ `en`: `name` справочника и есть английское название."""
    data = dict(localization or {})
    if not data.get("en") and name:
        data = {"en": name, **{k: v for k, v in data.items() if k != "en"}}
    return data
