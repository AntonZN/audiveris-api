"""failed_file_prepared: кадр, ушедший в движок, в архиве проблемных файлов

Отдельной ревизией, а не правкой 0009: та уже накатана на проде.

Старым строкам заполнить нечем — подготовленные кадры за прошлые провалы
вычищены уборкой по TTL вместе с рабочими каталогами.

Revision ID: 0010_failed_file_prepared
Revises: 0009_failed_file_reason
Create Date: 2026-09-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_failed_file_prepared"
down_revision: Union[str, None] = "0009_failed_file_reason"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("failed_files")}

    if "prepared_path" not in columns:
        op.add_column(
            "failed_files", sa.Column("prepared_path", sa.String(512), nullable=True)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("failed_files")}

    if "prepared_path" in columns:
        op.drop_column("failed_files", "prepared_path")
