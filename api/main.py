from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.config import settings
from api.repository import repo
from api.routes import router
from api.worker import Worker, create_workers

workers: list[Worker] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global workers
    # Startup: requeue running tasks and start workers
    repo.requeue_running_tasks()
    workers = create_workers(settings.task_workers)
    yield
    # Shutdown: stop workers gracefully
    for worker in workers:
        worker.stop()


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
