"""numeric_difficulty: значения difficulty 1, 2, 3 в публичном API

В Postgres SQLAlchemy Enum хранит стабильные имена easy/intermediate/hard,
а IntEnum сериализуется в API как 1/2/3.

Revision ID: 0004_numeric_difficulty
Revises: 0003_instrument_icon
Create Date: 2026-07-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_numeric_difficulty"
down_revision: Union[str, None] = "0003_instrument_icon"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_difficulty_constraints() -> None:
    op.execute("ALTER TABLE scores DROP CONSTRAINT IF EXISTS difficulty")
    op.execute(
        "ALTER TABLE scores DROP CONSTRAINT IF EXISTS ck_scores_difficulty"
    )


def upgrade() -> None:
    _drop_difficulty_constraints()
    op.execute(
        sa.text(
            "UPDATE scores SET difficulty = 'easy' "
            "WHERE difficulty = 'beginner'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE scores SET difficulty = 'hard' "
            "WHERE difficulty = 'advanced'"
        )
    )
    op.create_check_constraint(
        "ck_scores_difficulty",
        "scores",
        "difficulty IS NULL OR difficulty IN ('easy', 'intermediate', 'hard')",
    )


def downgrade() -> None:
    _drop_difficulty_constraints()
    op.execute(
        sa.text(
            "UPDATE scores SET difficulty = 'beginner' "
            "WHERE difficulty = 'easy'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE scores SET difficulty = 'advanced' "
            "WHERE difficulty = 'hard'"
        )
    )
    op.create_check_constraint(
        "ck_scores_difficulty",
        "scores",
        "difficulty IS NULL OR difficulty IN "
        "('beginner', 'intermediate', 'advanced')",
    )
