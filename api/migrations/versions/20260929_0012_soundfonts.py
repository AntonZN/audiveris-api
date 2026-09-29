"""soundfonts: звуки воспроизведения (.sf2), которые скачивает приложение

Revision ID: 0012_soundfonts
Revises: 0011_instrument_l10n
Create Date: 2026-09-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_soundfonts"
down_revision: Union[str, None] = "0011_instrument_l10n"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Baseline создаёт актуальную Base.metadata, поэтому на новой БД таблица
    # уже может существовать к моменту выполнения ревизии.
    if "soundfonts" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "soundfonts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("file", sa.String(), nullable=False),
        sa.Column("preview", sa.String(), nullable=True),
        sa.Column("localization", sa.JSON(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    if "soundfonts" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("soundfonts")
