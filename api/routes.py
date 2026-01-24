import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, Depends

from api.config import settings
from api.deps import get_api_key
from api.models import (
    HealthResponse,
    TaskCreateResponse,
    TaskResponse,
    TaskStatus,
)
from api.repository import repo

router = APIRouter(tags=["API"], dependencies=[Depends(get_api_key)])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(name: str, default: str) -> str:
    if not name:
        return default
    return Path(name).name or default


async def _save_file(file: UploadFile, path: Path) -> None:
    """Сохранить загруженный файл на диск."""
    with path.open("wb") as handle:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)


def _create_task_dirs(task_id: str) -> tuple[Path, Path]:
    """Создать директории для задачи."""
    input_dir = Path(settings.input_dir) / task_id
    output_dir = Path(settings.output_dir) / task_id
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return input_dir, output_dir


def _build_task(
    task_id: str,
    input_dir: Path,
    output_dir: Path,
    input_files: list[str],
    playlist: bool,
) -> dict:
    """Создать словарь задачи."""
    return {
        "id": task_id,
        "status": TaskStatus.queued.value,
        "created_at": _now(),
        "updated_at": _now(),
        "playlist": playlist,
        "input_files": input_files,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "progress": {"total": 1 if playlist else len(input_files), "completed": 0, "failed": 0},
        "results": [],
        "errors": [],
    }


@router.post(
    "/tasks/single",
    response_model=TaskCreateResponse,
    summary="Создать задачу (один файл)",
    description="""
Создать задачу на распознавание одного файла с нотами.

Загрузите один файл изображения (PNG, JPG) с нотами.
Задача будет добавлена в очередь на обработку Audiveris.
""",
    responses={
        200: {"description": "Задача успешно создана"},
    },
)
async def create_single_task(
    file: UploadFile = File(..., description="Файл изображения (PNG, JPG)"),
) -> TaskCreateResponse:
    """Создать задачу OMR для одного файла."""
    task_id = uuid.uuid4().hex
    input_dir, output_dir = _create_task_dirs(task_id)

    input_name = _safe_name(file.filename, "input-0")
    await _save_file(file, input_dir / input_name)

    task = _build_task(
        task_id=task_id,
        input_dir=input_dir,
        output_dir=output_dir,
        input_files=[input_name],
        playlist=False,
    )
    repo.save(task)
    repo.enqueue(task_id)

    return TaskCreateResponse(task_id=task_id, status=TaskStatus.queued)


@router.post(
    "/tasks/batch",
    response_model=TaskCreateResponse,
    summary="Создать задачу (плейлист)",
    description="""
Создать задачу на распознавание нескольких файлов как единого произведения.

Загрузите несколько файлов изображений (PNG, JPG) с нотами.
Все файлы будут объединены в один book и обработаны вместе.
На выходе — один MusicXML файл.
""",
    responses={
        200: {"description": "Задача успешно создана"},
        400: {"description": "Файлы не предоставлены"},
    },
)
async def create_batch_task(
    files: list[UploadFile] = File(..., description="Файлы изображений (PNG, JPG)"),
) -> TaskCreateResponse:
    """Создать задачу OMR для нескольких файлов (плейлист)."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    task_id = uuid.uuid4().hex
    input_dir, output_dir = _create_task_dirs(task_id)

    input_files: list[str] = []
    for i, file in enumerate(files):
        input_name = _safe_name(f"{i}-{file.filename}", f"input-{i}")
        await _save_file(file, input_dir / input_name)
        input_files.append(input_name)

    task = _build_task(
        task_id=task_id,
        input_dir=input_dir,
        output_dir=output_dir,
        input_files=input_files,
        playlist=True,
    )
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
    "/health",
    response_model=HealthResponse,
    summary="Проверка здоровья",
    description="Проверить статус API и текущую глубину очереди.",
)
async def health() -> HealthResponse:
    """Проверка здоровья API."""
    return HealthResponse(status="ok", queue_depth=repo.queue_depth())
