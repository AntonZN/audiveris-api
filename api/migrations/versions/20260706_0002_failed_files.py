"""failed_files: архив входов проваленных задач для аудита

Раньше входные файлы задачи, завершившейся ошибкой, удалялись вместе с временным
input_dir. Теперь они копируются в failures_dir, а метаданные (имя, путь копии,
текст ошибки, пресет и т.п.) кладутся в новую таблицу failed_files — чтобы в
админке провести аудит проблемных файлов. См. api/failures.py / api/worker.py.

Revision ID: 0002_failed_files
Revises: 0001_baseline
Create Date: 2026-07-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_failed_files"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "failed_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("preset", sa.String(length=32), nullable=True),
        sa.Column("enhance", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("stored_path", sa.String(length=512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("reviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_failed_files_created_at", "failed_files", ["created_at"]
    )
    op.create_index("ix_failed_files_task_id", "failed_files", ["task_id"])
    op.create_index("ix_failed_files_reviewed", "failed_files", ["reviewed"])


def downgrade() -> None:
    op.drop_index("ix_failed_files_reviewed", table_name="failed_files")
    op.drop_index("ix_failed_files_task_id", table_name="failed_files")
    op.drop_index("ix_failed_files_created_at", table_name="failed_files")
    op.drop_table("failed_files")
