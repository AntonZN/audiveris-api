"""pdmx_metadata: URL лицензии и исходные метаданные импортированных нот

Revision ID: 0007_pdmx_metadata
Revises: 0006_classical_tag
Create Date: 2026-08-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_pdmx_metadata"
down_revision: Union[str, None] = "0006_classical_tag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("scores")}

    if "license_url" not in columns:
        op.add_column(
            "scores",
            sa.Column("license_url", sa.String(length=512), nullable=True),
        )
    if "source_metadata" not in columns:
        op.add_column(
            "scores",
            sa.Column("source_metadata", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("scores")}

    if "source_metadata" in columns:
        op.drop_column("scores", "source_metadata")
    if "license_url" in columns:
        op.drop_column("scores", "license_url")
