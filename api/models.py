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


class TextDirection(ApiModel):
    """Текстовый <words> или <rehearsal> с привязкой к такту."""

    text: str = Field(description="Сам текст (напр. 'Adagio sostenuto', 'sempre pianissimo')")
    measure: str | None = Field(default=None, description="Номер такта, в котором найден")
    placement: str | None = Field(default=None, description="Положение: 'above' / 'below' (если указано Audiveris)")


class TextLyric(ApiModel):
    """Слоговой текст под нотой (<lyric><text>)."""

    text: str = Field(description="Слог (или склейка через elision)")
    measure: str | None = Field(default=None, description="Номер такта")


class ScoreTexts(ApiModel):
    """Все распознанные тексты из MusicXML. Сервер их вычищает из XML
    (мобильный плеер не умеет корректно их рисовать), но возвращает здесь,
    чтобы приложение само показало их над/вокруг плеера.
    """

    title: str | None = Field(default=None, description="Название (<movement-title>/<work-title>)")
    composer: str | None = Field(default=None, description="Композитор (<creator type='composer'>)")
    credits: list[str] = Field(default_factory=list, description="Визуальные подписи на странице (<credit-words>)")
    directions: list[TextDirection] = Field(default_factory=list, description="Текстовые ремарки в теле партитуры (<words>)")
    rehearsals: list[TextDirection] = Field(default_factory=list, description="Рехерсал-метки A/B/C (<rehearsal>)")
    lyrics: list[TextLyric] = Field(default_factory=list, description="Слоги под нотами (<lyric>)")
    part_names: list[str] = Field(default_factory=list, description="Имена партий (<part-name>)")
    instrument_names: list[str] = Field(default_factory=list, description="Имена инструментов (<instrument-name>)")


class ScoreAnalysis(ApiModel):
    """Метаданные партитуры от music21. Считаются всегда."""

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
    fixed: bool = Field(default=False, description="Был ли файл прогнан через music21 round-trip (true только если выход Audiveris не собирался в MIDI и его пришлось чинить)")
    bpm: int | None = Field(default=None, description="Темп начала песни (целое число BPM). None — Audiveris BPM не нашёл.")
    analysis: ScoreAnalysis | None = Field(default=None, description="Метаданные партитуры от music21 (тональность, размеры, список темпов, инструменты…)")
    texts: ScoreTexts | None = Field(default=None, description="Распознанные тексты, собранные ДО стрипа из XML. Мобила сама показывает их над плеером.")


class ValidateResponse(ApiModel):
    """Результат проверки «собирается ли MusicXML в MIDI через verovio»."""

    valid: bool = Field(description="Собрался ли файл в MIDI (после фикса, если был)")
    fixed: bool = Field(default=False, description="Был ли файл починен music21 (только при fix=true)")
    url: str | None = Field(default=None, description="Ссылка на рабочий файл (только при fix=true)")


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
