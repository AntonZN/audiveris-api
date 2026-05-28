from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def _to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(word.capitalize() for word in parts[1:])


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)


class TaskStatus(str, Enum):
    """Статус обработки задачи."""

    queued = "queued"
    running = "running"
    completed = "completed"
    error = "error"


class TaskProgress(ApiModel):
    """Прогресс обработки задачи."""

    total: int = Field(description="Общее количество файлов")
    completed: int = Field(description="Успешно обработано")
    failed: int = Field(description="Завершено с ошибкой")


class ScoreAnalysis(ApiModel):
    """Метаданные партитуры, извлечённые music21 (поле приходит при analyze=true)."""

    key: str | None = Field(default=None, description="Определённая тональность")
    key_confidence: float | None = Field(default=None, description="Уверенность определения тональности (0..1)")
    time_signatures: list[str] = Field(default_factory=list, description="Размеры (напр. '4/4')")
    tempos: list[float] = Field(default_factory=list, description="Найденные темпы (BPM)")
    parts: int = Field(default=0, description="Количество партий")
    instruments: list[str] = Field(default_factory=list, description="Инструменты партий")
    measures: int = Field(default=0, description="Количество тактов")
    notes: int = Field(default=0, description="Количество нот")


class FileResult(ApiModel):

    """Результат обработки одного файла."""

    filename: str = Field(description="Имя выходного файла")
    url: str | None = Field(default=None, description="Ссылка для скачивания")
    error: str | None = Field(default=None, description="Сообщение об ошибке")
    log_url: str | None = Field(default=None, description="Ссылка на лог Audiveris")
    fixed: bool = Field(default=False, description="Был ли файл починен music21 (при needFix=true)")
    analysis: ScoreAnalysis | None = Field(default=None, description="Метаданные партитуры (при analyze=true)")


class TaskCreateResponse(ApiModel):
    """Ответ после создания задачи."""

    task_id: str = Field(description="Уникальный идентификатор задачи")
    status: TaskStatus = Field(description="Начальный статус (всегда 'queued')")


class TaskResponse(ApiModel):
    """Ответ со статусом задачи."""

    id: str = Field(description="Уникальный идентификатор задачи")
    status: TaskStatus = Field(description="Текущий статус задачи")
    created_at: str | None = Field(default=None, description="Время создания (ISO 8601)")
    updated_at: str | None = Field(default=None, description="Время обновления (ISO 8601)")
    progress: TaskProgress | None = Field(default=None, description="Прогресс обработки")
    results: FileResult | None = Field(default=None, description="Результат обработки")
    errors: str | None = Field(default=None, description="Ошибка обработки")


class TaskResultResponse(ApiModel):
    """Ответ с результатом задачи."""

    task_id: str = Field(description="Идентификатор задачи")
    outputs: list[FileResult] = Field(description="Успешно обработанные файлы")
    errors: list[str] = Field(description="Список ошибок")


class HealthResponse(ApiModel):
    """Статус здоровья API."""

    status: str = Field(description="Статус ('ok')")
    queue_depth: int = Field(description="Количество задач в очереди")
