import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from api.config import settings
from api.models import (
    FileResult,
    HealthResponse,
    TaskCreateResponse,
    TaskResponse,
    TaskResultResponse,
    TaskStatus,
)
from api.repository import repo

router = APIRouter(tags=["Задачи OMR"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(name: str, default: str) -> str:
    if not name:
        return default
    return Path(name).name or default


@router.post(
    "/tasks",
    response_model=TaskCreateResponse,
    summary="Создать задачу",
    description="""
Создать новую задачу на оптическое распознавание музыкальных партитур (OMR).

Загрузите один или несколько файлов изображений (PNG, JPG, PDF) с нотами.
Задача будет добавлена в очередь на обработку Audiveris.

**Режимы обработки:**
- `playlist=false` (по умолчанию): Каждый файл обрабатывается независимо, на выходе отдельные MusicXML
- `playlist=true`: Все файлы объединяются в один book, на выходе один MusicXML

**Обработка ошибок:**
- `fail_on_error=true` (по умолчанию): Остановить обработку при первой ошибке
- `fail_on_error=false`: Продолжить обработку, вернуть частичный результат
    """,
    responses={
        200: {"description": "Задача успешно создана"},
        400: {"description": "Файлы не предоставлены"},
    },
)
async def create_task(
    files: list[UploadFile] = File(..., description="Файлы изображений (PNG, JPG, PDF)"),
    playlist: bool = Form(False, description="Объединить все файлы в один book"),
    fail_on_error: bool = Form(True, description="Остановить при первой ошибке"),
) -> TaskCreateResponse:
    """Создать новую задачу OMR."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    task_id = uuid.uuid4().hex
    input_dir = Path(settings.input_dir) / task_id
    output_dir = Path(settings.output_dir) / task_id
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    input_files: list[str] = []
    for i, file in enumerate(files):
        input_name = _safe_name(file.filename, f"input-{i}")
        input_path = input_dir / input_name
        with input_path.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        input_files.append(input_name)

    task = {
        "id": task_id,
        "status": TaskStatus.queued.value,
        "created_at": _now(),
        "updated_at": _now(),
        "playlist": playlist,
        "fail_on_error": fail_on_error,
        "input_files": input_files,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "progress": {"total": len(input_files) if not playlist else 1, "completed": 0, "failed": 0},
        "results": [],
        "errors": [],
    }
    repo.save(task)
    repo.enqueue(task_id)

    return TaskCreateResponse(task_id=task_id, status=TaskStatus.queued)


@router.get(
    "/tasks/{task_id}",
    response_model=TaskResponse,
    summary="Получить статус задачи",
    description="""
Получить текущий статус и детали задачи OMR.

Возвращает прогресс обработки, промежуточные результаты и ошибки.

**Статусы задачи:**
- `queued`: В очереди на обработку
- `running`: Обрабатывается
- `completed`: Успешно завершена
- `error`: Завершена с ошибкой
- `partial`: Частичный результат (при `fail_on_error=false`)
    """,
    responses={
        200: {"description": "Детали задачи"},
        404: {"description": "Задача не найдена"},
    },
)
async def get_task(task_id: str) -> TaskResponse:
    """Получить статус и детали задачи."""
    task = repo.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskResponse(
        id=task["id"],
        status=task["status"],
        created_at=task.get("created_at"),
        updated_at=task.get("updated_at"),
        progress=task.get("progress"),
        results=task.get("results", []),
        errors=task.get("errors", []),
    )


@router.get(
    "/tasks/{task_id}/result",
    response_model=TaskResultResponse,
    summary="Получить результат",
    description="""
Получить результат обработки завершённой задачи.

Доступно только для задач со статусом `completed` или `partial`.
Возвращает ссылки для скачивания MusicXML файлов.

**Формат вывода:**
- `.mxl`: Сжатый MusicXML (предпочтительный)
- `.xml`: Несжатый MusicXML
    """,
    responses={
        200: {"description": "Результат с ссылками на файлы"},
        404: {"description": "Задача не найдена"},
        409: {"description": "Задача ещё не завершена"},
    },
)
async def get_task_result(task_id: str) -> TaskResultResponse:
    """Получить результат (только для завершённых задач)."""
    task = repo.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    status = task.get("status")
    if status not in {TaskStatus.completed.value, TaskStatus.partial.value}:
        raise HTTPException(
            status_code=409, detail=f"Task not completed: {status}"
        )

    results = task.get("results", [])
    outputs = [FileResult(**r) for r in results if not r.get("error")]

    return TaskResultResponse(
        task_id=task_id,
        outputs=outputs,
        errors=task.get("errors", []),
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Проверка здоровья",
    description="Проверить статус API и текущую глубину очереди.",
    tags=["Система"],
)
async def health() -> HealthResponse:
    """Проверка здоровья API."""
    return HealthResponse(status="ok", queue_depth=repo.queue_depth())
