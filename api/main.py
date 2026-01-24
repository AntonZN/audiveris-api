from contextlib import asynccontextmanager

from fastapi import FastAPI

import threading

from api.cleanup import start_cleanup_loop
from api.config import settings
from api.repository import repo
from api.routes import router
from api.worker import Worker, create_workers

workers: list[Worker] = []
cleanup_stop_event = threading.Event()
cleanup_thread: threading.Thread | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global workers, cleanup_thread
    # Startup: requeue running tasks and start workers
    repo.requeue_running_tasks()
    workers = create_workers(settings.task_workers)
    if settings.task_ttl_seconds > 0:
        cleanup_stop_event.clear()
        cleanup_thread = start_cleanup_loop(cleanup_stop_event)
    yield
    # Shutdown: stop workers gracefully
    for worker in workers:
        worker.stop()
    cleanup_stop_event.set()
    if cleanup_thread:
        cleanup_thread.join(timeout=2)


app = FastAPI(
    title="Audiveris OMR API",
    description="""
Асинхронный REST API для оптического распознавания музыкальных партитур (OMR) с использованием Audiveris.

## Возможности

- **Одиночная обработка**: Загрузите изображение — получите MusicXML
- **Пакетная обработка**: Загрузите несколько файлов — каждый обрабатывается независимо
- **Режим playlist**: Объединение нескольких страниц в один MusicXML
- **Асинхронная обработка**: Очередь задач через Redis
- **Отслеживание прогресса**: Мониторинг статуса и прогресса задач

## Порядок работы

1. **POST /tasks** — Создать задачу на распознавание
2. **GET /tasks/{task_id}** — Проверить статус задачи
3. **GET /tasks/{task_id}/result** — Получить ссылки на результаты
    """,
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(router)
