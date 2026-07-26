"""instrument_icon: иконка инструмента для админки и публичного API

Revision ID: 0003_instrument_icon
Revises: 0002_failed_files
Create Date: 2026-07-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_instrument_icon"
down_revision: Union[str, None] = "0002_failed_files"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Baseline создаёт таблицы из актуальной Base.metadata, поэтому на новой БД
    # колонка уже может существовать к моменту выполнения этой ревизии.
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("instruments")
    }
    if "icon" not in columns:
        op.add_column(
            "instruments",
            sa.Column("icon", sa.String(), nullable=True),
        )


def downgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("instruments")
    }
    if "icon" in columns:
        op.drop_column("instruments", "icon")
