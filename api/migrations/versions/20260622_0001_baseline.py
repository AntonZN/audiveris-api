"""baseline: текущая схема каталога и статистики

Базовая ревизия для внедрения Alembic в уже существующий (greenfield) проект.
Схема описана ORM-моделями, поэтому baseline создаёт её целиком из
Base.metadata (checkfirst=True — идемпотентно: на свежей БД создаст всё, на уже
поднятой ничего не тронет). Последующие изменения — обычным
`alembic revision --autogenerate`.

ВАЖНО: если БД уже была создана старым `init_db()`/create_all, выполните один раз
`alembic -c api/alembic.ini stamp head`, чтобы пометить её как мигрированную и не
прогонять baseline.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-22
"""
from typing import Sequence, Union

from alembic import op

from api.db import Base

# Импорт моделей регистрирует таблицы в Base.metadata.
from api import catalog_models  # noqa: F401
from api import stats_models  # noqa: F401

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
