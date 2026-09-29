"""instrument_localization: переводы названий инструментов

`localization` — JSON {код языка: перевод}; существующим инструментам
заполняем `en` из `name` (name и так английский).

Revision ID: 0011_instrument_l10n
Revises: 0010_failed_file_prepared
Create Date: 2026-09-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_instrument_l10n"
down_revision: Union[str, None] = "0010_failed_file_prepared"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Baseline создаёт актуальную Base.metadata, поэтому на новой БД колонка
    # уже может существовать к моменту выполнения ревизии.
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("instruments")}

    if "localization" not in columns:
        op.add_column("instruments", sa.Column("localization", sa.JSON(), nullable=True))

    instruments = sa.table(
        "instruments",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("localization", sa.JSON),
    )
    rows = bind.execute(
        sa.select(instruments.c.id, instruments.c.name).where(
            instruments.c.localization.is_(None)
        )
    ).all()
    for instrument_id, name in rows:
        bind.execute(
            instruments.update()
            .where(instruments.c.id == instrument_id)
            .values(localization={"en": name})
        )


def downgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("instruments")
    }
    if "localization" in columns:
        op.drop_column("instruments", "localization")
