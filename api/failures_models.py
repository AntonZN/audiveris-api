"""Модель «архива провалов» OMR.

Одна строка = один входной файл задачи, которую не удалось обработать. В отличие
от `ProcessingEvent` (агрегатная статистика, см. api/stats_models.py) здесь мы
храним САМ файл (копию во внешней failures_dir) и текст ошибки — чтобы позже в
админке провести аудит «с какими файлами мы работаем плохо» и на реальных
примерах улучшать распознавание. Пишется воркером через api/failures.py вместо
того, чтобы молча удалить входные файлы вместе с временным input_dir.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class FailedFile(Base):
    __tablename__ = "failed_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # single | playlist
    preset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enhance: Mapped[bool] = mapped_column(Boolean, default=False)
    # Исходное имя файла, как его загрузил клиент.
    filename: Mapped[str] = mapped_column(String(255))
    # Абсолютный путь сохранённой копии внутри failures_dir (лежит под media_root,
    # поэтому раздаётся /media/*). None — копию сохранить не удалось (напр. файла
    # уже не было на диске), но строку об ошибке всё равно заводим.
    stored_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Текст ошибки обработки (усечён до max_error_len при записи).
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Флаг для ручного аудита: админ отмечает разобранные случаи.
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    def __str__(self) -> str:
        return f"{self.filename} ({self.kind}) @ {self.created_at}"
