"""score_is_broken: флаг невалидного MusicXML для нот

Revision ID: 0008_score_is_broken
Revises: 0007_pdmx_metadata
Create Date: 2026-09-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_score_is_broken"
down_revision: Union[str, None] = "0007_pdmx_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("scores")}
    indexes = {index["name"] for index in inspector.get_indexes("scores")}

    if "is_broken" not in columns:
        op.add_column(
            "scores",
            sa.Column(
                "is_broken",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if "ix_scores_is_broken" not in indexes:
        op.create_index("ix_scores_is_broken", "scores", ["is_broken"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("scores")}
    columns = {column["name"] for column in inspector.get_columns("scores")}

    if "ix_scores_is_broken" in indexes:
        op.drop_index("ix_scores_is_broken", table_name="scores")
    if "is_broken" in columns:
        op.drop_column("scores", "is_broken")
