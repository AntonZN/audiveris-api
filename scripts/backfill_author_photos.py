#!/usr/bin/env python3
"""Дозалить фото авторов (Author.photo) из Wikidata P18 -> Wikimedia Commons.

Работает ТОЛЬКО по данным БД: берёт авторов без фото, у которых заполнен
wikidata, и тянет портрет. TSV/локальный клон/GitHub НЕ нужны — только доступ к
wikidata.org и commons.wikimedia.org. Для случая, когда импорт был сделан с
--no-photos и фото нужно догрузить отдельно.

Идемпотентен: авторов с уже привязанным фото пропускает, можно гонять повторно.

    docker compose exec audiveris-api python scripts/backfill_author_photos.py
    docker compose exec audiveris-api python scripts/backfill_author_photos.py --limit 20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Чтобы работали и `import api...`, и `import import_lieder` (лежит рядом).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from api.catalog_models import Author  # noqa: E402
from api.db import SessionLocal  # noqa: E402

# Переиспользуем готовую логику загрузки/ресайза фото из импортёра.
from import_lieder import _fetch_photo  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Дозалить фото авторов из Wikidata")
    parser.add_argument("--limit", type=int, default=None, help="Обработать не более N авторов")
    args = parser.parse_args()

    # User-Agent обязателен для Wikimedia/Wikidata (иначе 403).
    client = httpx.Client(
        timeout=60.0,
        follow_redirects=True,
        headers={"User-Agent": "audiveris-catalog-importer/1.0 (dev@dev.com)"},
    )
    db = SessionLocal()
    try:
        # Сравнивать Author.photo с "" нельзя: пустую строку прогоняет бинд-
        # процессор ImageType, который ждёт upload-объект. Незаполненное фото —
        # это NULL, поэтому фильтруем только по IS NULL.
        stmt = select(Author).where(
            Author.photo.is_(None),
            Author.wikidata.isnot(None),
            Author.wikidata != "",
        )
        if args.limit:
            stmt = stmt.limit(args.limit)
        authors = db.execute(stmt).scalars().all()
        print(f"Авторов без фото с заполненным wikidata: {len(authors)}")

        ok = 0
        missing = 0
        for i, author in enumerate(authors, 1):
            if _fetch_photo(client, author, "", author.wikidata or ""):
                ok += 1
            else:
                missing += 1
            if i % 25 == 0:
                db.commit()  # пишет привязанные фото в хранилище
                print(f"  ...{i}/{len(authors)} (фото={ok})")
        db.commit()
        print(f"Готово: фото загружено={ok}, без портрета/ошибка={missing}")
    finally:
        client.close()
        db.close()


if __name__ == "__main__":
    main()
