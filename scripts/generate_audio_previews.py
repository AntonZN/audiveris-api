#!/usr/bin/env python3
"""Догенерировать отсутствующие MP3-превью для нот каталога.

Пример::

    docker compose exec audiveris-api \
        python scripts/generate_audio_previews.py --limit 50

Скрипт идемпотентен: выбирает только Score с music_file и без audio_file.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from sqlalchemy import select

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from api.audio_preview import render_audio_preview  # noqa: E402
from api.catalog_models import Score  # noqa: E402
from api.db import SessionLocal, init_db  # noqa: E402


class _BytesUpload:
    def __init__(self, content: bytes, filename: str) -> None:
        self.file = io.BytesIO(content)
        self.filename = filename


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сгенерировать отсутствующие MP3-превью из MusicXML/MXL"
    )
    parser.add_argument("--limit", type=int, default=None, help="Обработать не более N нот")
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Длительность превью в секундах (по умолчанию из конфигурации)",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=25,
        help="Фиксировать транзакцию каждые N успешно созданных превью",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration must be > 0")
    if args.commit_every < 1:
        parser.error("--commit-every must be >= 1")
    return args


def main() -> None:
    args = parse_args()
    init_db()

    stmt = (
        select(Score)
        .where(Score.music_file.is_not(None), Score.audio_file.is_(None))
        .order_by(Score.id)
    )
    if args.limit is not None:
        stmt = stmt.limit(args.limit)

    db = SessionLocal()
    generated = 0
    failed = 0
    missing = 0
    try:
        scores = db.execute(stmt).scalars().all()
        print(f"Найдено нот без аудиопревью: {len(scores)}")
        for score in scores:
            db.refresh(score, attribute_names=["music_file"])
            music_path = Path(str(score.music_file))
            if not music_path.is_file():
                missing += 1
                print(f"  [{score.id}] отсутствует music_file: {music_path}")
                continue

            mp3 = render_audio_preview(music_path, duration=args.duration)
            if not mp3:
                failed += 1
                print(f"  [{score.id}] не удалось создать превью: {score.title}")
                continue

            score.audio_file = _BytesUpload(mp3, f"score-{score.id}-preview.mp3")
            db.flush()
            generated += 1
            print(f"  [{score.id}] готово: {score.title}")
            if generated % args.commit_every == 0:
                db.commit()

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(f"Готово: создано={generated}, ошибок={failed}, отсутствует файлов={missing}")


if __name__ == "__main__":
    main()
