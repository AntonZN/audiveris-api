"""failed_file_reason: причина провала и лог обработки в архиве проблемных файлов

Старые строки не оставляем пустыми: прогоняем их текст ошибки через тот же
классификатор, что и новые. Иначе метрика по причинам стартует с нуля, а вся
накопленная история провалов остаётся мёртвым текстом.

Revision ID: 0009_failed_file_reason
Revises: 0008_score_is_broken
Create Date: 2026-09-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from api.failure_reasons import classify

revision: str = "0009_failed_file_reason"
down_revision: Union[str, None] = "0008_score_is_broken"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("failed_files")}
    indexes = {index["name"] for index in inspector.get_indexes("failed_files")}

    if "reason" not in columns:
        op.add_column("failed_files", sa.Column("reason", sa.String(32), nullable=True))
    if "log_path" not in columns:
        # Старым строкам заполнить нечем: их логи давно вычищены по TTL.
        op.add_column("failed_files", sa.Column("log_path", sa.String(512), nullable=True))
    if "ix_failed_files_reason" not in indexes:
        op.create_index("ix_failed_files_reason", "failed_files", ["reason"])

    # Задним числом раскладываем уже накопленные провалы по причинам.
    rows = bind.execute(
        sa.text("SELECT id, error FROM failed_files WHERE reason IS NULL")
    ).fetchall()
    for row_id, error in rows:
        bind.execute(
            sa.text("UPDATE failed_files SET reason = :reason WHERE id = :id"),
            {"reason": classify(error), "id": row_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("failed_files")}
    columns = {column["name"] for column in inspector.get_columns("failed_files")}

    if "ix_failed_files_reason" in indexes:
        op.drop_index("ix_failed_files_reason", table_name="failed_files")
    if "log_path" in columns:
        op.drop_column("failed_files", "log_path")
    if "reason" in columns:
        op.drop_column("failed_files", "reason")
