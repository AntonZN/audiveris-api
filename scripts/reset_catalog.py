#!/usr/bin/env python3
"""Очистка КАТАЛОГА для тестов: удаляет все данные каталога из БД и медиа-файлы.

Затрагивает только таблицы каталога (ноты/подборки/авторы/жанры/стили/инструменты,
пользователи приложения, оценки, проигрывания) и директорию CATALOG_MEDIA_DIR.
OMR-задачи в Redis и их файлы НЕ трогает.

Без `--yes` ничего не удаляет — только показывает, что будет удалено.

Примеры
-------
    # посмотреть, что есть (dry-run):
    docker compose exec audiveris-api python scripts/reset_catalog.py

    # очистить данные (TRUNCATE + сброс id на Postgres) и удалить медиа:
    docker compose exec audiveris-api python scripts/reset_catalog.py --yes

    # очистить только БД, медиа оставить:
    docker compose exec audiveris-api python scripts/reset_catalog.py --yes --keep-media

    # полностью пересоздать таблицы (DROP + CREATE):
    docker compose exec audiveris-api python scripts/reset_catalog.py --yes --drop
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Чтобы `import api...` работал и при запуске как ./scripts/reset_catalog.py.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import func, select, text  # noqa: E402

from api import catalog_models  # noqa: E402,F401  (регистрирует таблицы в metadata)
from api import stats_models  # noqa: E402,F401  (чтобы знать имя таблицы статистики)
from api.config import settings  # noqa: E402
from api.db import Base, engine, init_db, run_migrations  # noqa: E402

# Статистика OMR живёт постоянно — НЕ чистим её при сбросе каталога.
STATS_TABLES = {stats_models.ProcessingEvent.__tablename__}
# sorted_tables: родители -> дети (для удаления идём в обратном порядке).
CATALOG_TABLES = [t for t in Base.metadata.sorted_tables if t.name not in STATS_TABLES]


def counts() -> dict[str, int]:
    with engine.connect() as conn:
        return {
            t.name: conn.execute(select(func.count()).select_from(t)).scalar() or 0
            for t in CATALOG_TABLES
        }


def wipe_db(reset_ids: bool) -> None:
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            names = ", ".join(f'"{t.name}"' for t in CATALOG_TABLES)
            restart = "RESTART IDENTITY " if reset_ids else ""
            conn.execute(text(f"TRUNCATE {names} {restart}CASCADE"))
        else:
            # SQLite и прочие: удаляем детей раньше родителей.
            for t in reversed(CATALOG_TABLES):
                conn.execute(t.delete())


def drop_and_recreate() -> None:
    Base.metadata.drop_all(bind=engine)
    # Сбрасываем версию Alembic: иначе `upgrade head` решит, что схема уже на
    # месте, и не пересоздаст таблицы. После — накатываем миграции с нуля, чтобы
    # единственным источником схемы оставался Alembic.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    run_migrations()


def wipe_media() -> int:
    root = Path(settings.catalog_media_dir)
    removed = 0
    if root.is_dir():
        for p in root.iterdir():
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            removed += 1
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Очистить данные каталога (для тестов)")
    parser.add_argument("--yes", action="store_true", help="Подтвердить удаление (иначе dry-run)")
    parser.add_argument("--keep-media", action="store_true", help="Не удалять файлы в CATALOG_MEDIA_DIR")
    parser.add_argument("--no-reset-ids", action="store_true", help="Не сбрасывать счётчики id (Postgres)")
    parser.add_argument("--drop", action="store_true", help="DROP + CREATE таблиц вместо очистки строк")
    args = parser.parse_args()

    init_db()  # гарантируем, что таблицы существуют
    before = counts()
    total = sum(before.values())

    print(f"БД: {settings.database_url.rsplit('@', 1)[-1]}")
    print(f"Медиа: {settings.catalog_media_dir}")
    print("Текущие данные каталога:")
    for name, n in before.items():
        print(f"  {name:20} {n}")
    print(f"  ИТОГО строк: {total}")

    if not args.yes:
        print("\nDry-run. Ничего не удалено. Запустите с --yes, чтобы очистить.")
        return

    if args.drop:
        drop_and_recreate()
        print("\nТаблицы каталога пересозданы (DROP + CREATE).")
    else:
        wipe_db(reset_ids=not args.no_reset_ids)
        print("\nДанные каталога удалены.")

    if args.keep_media:
        print("Медиа-файлы оставлены (--keep-media).")
    else:
        removed = wipe_media()
        print(f"Удалено медиа-объектов: {removed}")


if __name__ == "__main__":
    main()
