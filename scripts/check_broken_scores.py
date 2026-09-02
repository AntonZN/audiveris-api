#!/usr/bin/env python3
"""Проверить все MusicXML/MXL в каталоге через Verovio и пометить битые ноты.

Verovio запускается через ``api.verovio_check.midi_ok`` в отдельном процессе,
поэтому segmentation fault внутри ``renderToMIDI`` не роняет сам скрипт.

Запуск в Docker::

    docker compose exec audiveris-api python scripts/check_broken_scores.py

Нота без ``music_file`` или с отсутствующим файлом тоже считается битой: её
невозможно проверить через ``renderToMIDI``. Повторный запуск пересчитывает флаг
и сбрасывает ``is_broken`` в false, если ранее проблемная нота стала рабочей.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from api.catalog_models import Score  # noqa: E402
from api.db import SessionLocal, init_db  # noqa: E402
from api.verovio_check import midi_ok  # noqa: E402

VEROVIO_TIMEOUT_SECONDS = 15


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить MusicXML/MXL через Verovio и обновить is_broken"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Обработать не более N партитур (по умолчанию — все)",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=25,
        help="Фиксировать изменения каждые N проверок",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.commit_every < 1:
        parser.error("--commit-every must be >= 1")
    return args


def check_score(score: Score) -> tuple[bool, str]:
    """Вернуть (is_ok, reason) для одной записи каталога."""
    if not score.music_file:
        return False, "music_file не привязан"

    music_path = Path(str(score.music_file))
    if not music_path.is_file():
        return False, f"файл отсутствует: {music_path}"

    if midi_ok(music_path, timeout=VEROVIO_TIMEOUT_SECONDS):
        return True, "renderToMIDI ok"
    return False, "renderToMIDI не прошёл или истёк таймаут 15 секунд"


def main() -> None:
    args = parse_args()
    init_db()

    stmt = select(Score).order_by(Score.id)
    if args.limit is not None:
        stmt = stmt.limit(args.limit)

    checked = 0
    marked_broken = 0
    repaired = 0
    unchanged = 0

    with SessionLocal() as db:
        try:
            scores = db.execute(stmt).scalars().all()
            print(f"Найдено партитур: {len(scores)}")

            for score in scores:
                # FileType хранит путь к файлу; обновляем значение перед проверкой.
                db.refresh(score, attribute_names=["music_file"])
                is_ok, reason = check_score(score)
                is_broken = not is_ok

                if score.is_broken != is_broken:
                    score.is_broken = is_broken
                    if is_broken:
                        marked_broken += 1
                    else:
                        repaired += 1
                else:
                    unchanged += 1

                checked += 1
                status = "BROKEN" if is_broken else "OK"
                print(f"  [{status}] [{score.id}] {score.title}: {reason}")

                if checked % args.commit_every == 0:
                    db.commit()

            db.commit()
        except Exception:
            db.rollback()
            raise

    print(
        "Готово: "
        f"проверено={checked}, новых битых={marked_broken}, "
        f"восстановлено={repaired}, без изменений={unchanged}"
    )


if __name__ == "__main__":
    main()
