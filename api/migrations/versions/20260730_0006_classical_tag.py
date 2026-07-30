"""classical_tag: назначить тег classical всем существующим нотам

Revision ID: 0006_classical_tag
Revises: 0005_score_tags
Create Date: 2026-07-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_classical_tag"
down_revision: Union[str, None] = "0005_score_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TAG_NAME = "classical"
_TAG_SLUG = "classical"


def upgrade() -> None:
    connection = op.get_bind()

    # Не создаём дубликат, если тег уже был добавлен вручную до миграции.
    connection.execute(
        sa.text(
            """
            INSERT INTO tags (name, slug)
            SELECT :name, :slug
            WHERE NOT EXISTS (
                SELECT 1 FROM tags WHERE slug = :slug
            )
            """
        ),
        {"name": _TAG_NAME, "slug": _TAG_SLUG},
    )

    # Назначаем classical каждой существующей ноте. NOT EXISTS делает операцию
    # безопасной для повторного запуска и сохраняет уже созданные связи.
    connection.execute(
        sa.text(
            """
            INSERT INTO score_tags (score_id, tag_id)
            SELECT scores.id, tags.id
            FROM scores
            JOIN tags ON tags.slug = :slug
            WHERE NOT EXISTS (
                SELECT 1
                FROM score_tags
                WHERE score_tags.score_id = scores.id
                  AND score_tags.tag_id = tags.id
            )
            """
        ),
        {"slug": _TAG_SLUG},
    )


def downgrade() -> None:
    connection = op.get_bind()

    connection.execute(
        sa.text(
            """
            DELETE FROM score_tags
            WHERE tag_id IN (
                SELECT id FROM tags WHERE slug = :slug
            )
            """
        ),
        {"slug": _TAG_SLUG},
    )
    connection.execute(
        sa.text("DELETE FROM tags WHERE slug = :slug"),
        {"slug": _TAG_SLUG},
    )
