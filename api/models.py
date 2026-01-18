from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Статус обработки задачи."""

    queued = "queued"
    running = "running"
    completed = "completed"
    error = "error"
    partial = "partial"


class TaskProgress(BaseModel):
    """Прогресс обработки задачи."""

    total: int = Field(description="Общее количество файлов")
    completed: int = Field(description="Успешно обработано")
    failed: int = Field(description="Завершено с ошибкой")


class FileResult(BaseModel):
    """Результат обработки одного файла."""

    filename: str = Field(description="Имя выходного файла")
    url: str | None = Field(default=None, description="Ссылка для скачивания")
    error: str | None = Field(default=None, description="Сообщение об ошибке")
    error_code: str | None = Field(
        default=None, description="Код ошибки: 'low_interline' или 'processing_failed'"
    )
    interline: int | None = Field(
        default=None, description="Определённое значение interline в пикселях"
    )
    log_url: str | None = Field(default=None, description="Ссылка на лог Audiveris")


class TaskCreateResponse(BaseModel):
    """Ответ после создания задачи."""

    task_id: str = Field(description="Уникальный идентификатор задачи")
    status: TaskStatus = Field(description="Начальный статус (всегда 'queued')")


class Task(BaseModel):
    """Полная модель задачи (внутреннее использование)."""

    id: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    playlist: bool = False
    fail_on_error: bool = True
    input_files: list[str] = []
    input_dir: str | None = None
    output_dir: str | None = None
    progress: TaskProgress | None = None
    results: list[FileResult] = []
    errors: list[str] = []


class TaskResponse(BaseModel):
    """Ответ со статусом задачи."""

    id: str = Field(description="Уникальный идентификатор задачи")
    status: TaskStatus = Field(description="Текущий статус задачи")
    created_at: str | None = Field(default=None, description="Время создания (ISO 8601)")
    updated_at: str | None = Field(default=None, description="Время обновления (ISO 8601)")
    progress: TaskProgress | None = Field(default=None, description="Прогресс обработки")
    results: list[FileResult] = Field(default=[], description="Результаты по каждому файлу")
    errors: list[str] = Field(default=[], description="Список ошибок")


class TaskResultResponse(BaseModel):
    """Ответ с результатом задачи."""

    task_id: str = Field(description="Идентификатор задачи")
    outputs: list[FileResult] = Field(description="Успешно обработанные файлы")
    errors: list[str] = Field(description="Список ошибок")


class HealthResponse(BaseModel):
    """Статус здоровья API."""

    status: str = Field(description="Статус ('ok')")
    queue_depth: int = Field(description="Количество задач в очереди")
