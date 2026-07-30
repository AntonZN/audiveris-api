"""score_tags: справочник тегов и many-to-many связь с нотами

Revision ID: 0005_score_tags
Revises: 0004_numeric_difficulty
Create Date: 2026-07-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_score_tags"
down_revision: Union[str, None] = "0004_numeric_difficulty"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Baseline создаёт актуальную Base.metadata целиком, поэтому на новой БД
    # эти таблицы уже могут существовать к моменту выполнения ревизии.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "tags" not in tables:
        op.create_table(
            "tags",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("slug", sa.String(length=140), nullable=False),
        )
        op.create_index("ix_tags_slug", "tags", ["slug"], unique=True)

    if "score_tags" not in tables:
        op.create_table(
            "score_tags",
            sa.Column(
                "score_id",
                sa.Integer(),
                sa.ForeignKey("scores.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "tag_id",
                sa.Integer(),
                sa.ForeignKey("tags.id", ondelete="CASCADE"),
                primary_key=True,
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "score_tags" in tables:
        op.drop_table("score_tags")
    if "tags" in tables:
        indexes = {
            index["name"] for index in inspector.get_indexes("tags")
        }
        if "ix_tags_slug" in indexes:
            op.drop_index("ix_tags_slug", table_name="tags")
        op.drop_table("tags")
