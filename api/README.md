# Audiveris API

Асинхронный REST API для оптического распознавания музыкальных партитур (OMR) с использованием Audiveris.

## Архитектура

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Client    │────▶│  FastAPI    │────▶│    Redis    │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │
                                               ▼
                                        ┌─────────────┐
                                        │   Worker    │
                                        └──────┬──────┘
                                               │
                                               ▼
                                        ┌─────────────┐
                                        │  Audiveris  │
                                        └─────────────┘
```

- **FastAPI** — принимает запросы, сохраняет задачи в Redis, возвращает статус
- **Redis** — хранилище задач и очередь на обработку
- **Worker** — фоновый обработчик, берёт задачи из очереди и запускает Audiveris
- **Audiveris** — Java-приложение для распознавания нот

## Структура файлов

| Файл | Описание |
|------|----------|
| `main.py` | Точка входа FastAPI, lifespan (startup/shutdown) |
| `config.py` | Настройки из переменных окружения (pydantic-settings) |
| `models.py` | Pydantic модели для request/response |
| `routes.py` | HTTP эндпоинты |
| `repository.py` | Работа с Redis (CRUD задач, очередь) |
| `services.py` | Бизнес-логика Audiveris |
| `worker.py` | Фоновый обработчик очереди |
| `exceptions.py` | Кастомные исключения |

## API Endpoints

### POST /tasks

Создание задачи на распознавание.

**Request:**
- `files` (multipart/form-data) — один или несколько файлов изображений
- `playlist` (bool, default: false) — объединить файлы в один book (один MusicXML на выходе)
- `fail_on_error` (bool, default: true) — остановить обработку при первой ошибке

**Response:**
```json
{
  "task_id": "abc123def456",
  "status": "queued"
}
```

**Примеры:**

```bash
# Один файл
curl -X POST -F "files=@score.png" http://localhost:8000/tasks

# Несколько файлов (каждый обрабатывается отдельно)
curl -X POST \
  -F "files=@page1.png" \
  -F "files=@page2.png" \
  http://localhost:8000/tasks

# Playlist (все страницы → один MusicXML)
curl -X POST \
  -F "files=@page1.png" \
  -F "files=@page2.png" \
  -F "playlist=true" \
  http://localhost:8000/tasks

# Продолжить при ошибках (partial result)
curl -X POST \
  -F "files=@page1.png" \
  -F "files=@page2.png" \
  -F "fail_on_error=false" \
  http://localhost:8000/tasks
```

---

### GET /tasks/{task_id}

Получение статуса и деталей задачи.

**Response:**
```json
{
  "id": "abc123def456",
  "status": "running",
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T10:30:05Z",
  "progress": {
    "total": 3,
    "completed": 1,
    "failed": 0
  },
  "results": [
    {
      "filename": "page1.mxl",
      "url": "http://localhost:8081/out/abc123/page1.mxl",
      "interline": 15,
      "log_url": "http://localhost:8081/out/abc123/page1.log"
    }
  ],
  "errors": []
}
```

**Статусы задачи:**

| Статус | Описание |
|--------|----------|
| `queued` | В очереди на обработку |
| `running` | Обрабатывается |
| `completed` | Успешно завершена |
| `error` | Завершена с ошибкой |
| `partial` | Частичный результат (fail_on_error=false) |

**Пример:**
```bash
curl http://localhost:8000/tasks/abc123def456
```

---

### GET /tasks/{task_id}/result

Получение результата (только для `completed` или `partial`).

**Response:**
```json
{
  "task_id": "abc123def456",
  "outputs": [
    {
      "filename": "score.mxl",
      "url": "http://localhost:8081/out/abc123/score.mxl",
      "interline": 15,
      "log_url": "http://localhost:8081/out/abc123/score.log"
    }
  ],
  "errors": []
}
```

**Коды ошибок:**
- `404` — задача не найдена
- `409` — задача ещё не завершена

**Пример:**
```bash
curl http://localhost:8000/tasks/abc123def456/result
```

---

### GET /health

Health check.

**Response:**
```json
{
  "status": "ok",
  "queue_depth": 5
}
```

**Пример:**
```bash
curl http://localhost:8000/health
```

## Переменные окружения

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `AUDIVERIS_CMD` | `audiveris` | Путь к исполняемому файлу |
| `AUDIVERIS_ARGS` | `-batch -transcribe -export` | Аргументы командной строки |
| `INPUT_DIR` | `/storage/in` | Директория для входных файлов |
| `OUTPUT_DIR` | `/storage/out` | Директория для результатов |
| `REDIS_URL` | `redis://redis:6379/0` | URL подключения к Redis |
| `TASK_WORKERS` | `1` | Количество воркеров |
| `MIN_INTERLINE` | `11` | Минимальный interline (пикселей) |
| `MEDIA_ROOT` | `/storage` | Корень для построения URL |
| `MEDIA_BASE_URL` | `http://localhost:8081` | Базовый URL для файлов |
| `MEDIA_PATH_PREFIX` | `` | Префикс пути в URL |
| `KEEP_ARTIFACTS` | `1` | Сохранять артефакты после обработки |
| `REQUEUE_RUNNING` | `1` | Перезапускать running задачи при старте |

## Обработка ошибок

### low_interline

Разрешение изображения слишком низкое. Audiveris требует минимальный interline (расстояние между линиями нотоносца) для корректного распознавания.

```json
{
  "filename": "score.png",
  "error": "Image resolution too low: interline=8px < 11px",
  "error_code": "low_interline",
  "interline": 8
}
```

### processing_failed

Общая ошибка обработки Audiveris.

```json
{
  "filename": "score.png",
  "error": "Audiveris failed (job_id=abc123). No sheet found",
  "error_code": "processing_failed"
}
```

## Режимы обработки

### Одиночные файлы (playlist=false)

Каждый файл обрабатывается независимо → каждый даёт свой MusicXML.

```
files: [page1.png, page2.png, page3.png]
      ↓
results: [page1.mxl, page2.mxl, page3.mxl]
```

### Playlist (playlist=true)

Все файлы объединяются в один book → один MusicXML.

```
files: [page1.png, page2.png, page3.png]
      ↓
results: [playlist.mxl]
```

Используйте playlist для многостраничных партитур.

**Внутренний процесс playlist (3 шага):**

```
Step 1: Транскрибирование каждого файла
        page1.png → page1.omr
        page2.png → page2.omr
        page3.png → page3.omr

Step 2: Создание compound book
        playlist.xml (ссылки на .omr) → playlist.omr

Step 3: Экспорт
        playlist.omr → playlist.mxl
```

Такой подход гарантирует, что каждый файл полностью обработан перед объединением.

## Запуск

```bash
# Через docker-compose
docker compose up -d

# Локально (для разработки)
uvicorn api.main:app --reload
```
