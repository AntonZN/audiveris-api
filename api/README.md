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

### POST /tasks/single

Создание задачи на распознавание **одного файла**.

**Request:**
- `file` (multipart/form-data) — один файл изображения (PNG, JPG, PDF)

**Response:**
```json
{
  "task_id": "abc123def456",
  "status": "queued"
}
```

**Пример:**

```bash
curl -X POST -F "file=@score.png" http://localhost:8000/tasks/single
```

---

### POST /tasks/batch

Создание задачи на распознавание **нескольких файлов** как единого произведения (плейлист).

Все файлы объединяются в один book → на выходе один MusicXML.

**Request:**
- `files` (multipart/form-data) — несколько файлов изображений (PNG, JPG, PDF)

**Response:**
```json
{
  "task_id": "abc123def456",
  "status": "queued"
}
```

**Пример:**

```bash
curl -X POST \
  -F "files=@page1.png" \
  -F "files=@page2.png" \
  -F "files=@page3.png" \
  http://localhost:8000/tasks/batch
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
    "total": 1,
    "completed": 0,
    "failed": 0
  },
  "results": [],
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

**Пример:**
```bash
curl http://localhost:8000/tasks/abc123def456
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
  "error": "Audiveris failed. No sheet found",
  "error_code": "processing_failed"
}
```

## Режимы обработки

### /tasks/single — Один файл

Один файл → один MusicXML.

```
file: score.png
      ↓
result: score.mxl
```

### /tasks/batch — Плейлист (несколько файлов)

Все файлы объединяются в один book → один MusicXML.

```
files: [page1.png, page2.png, page3.png]
      ↓
result: playlist.mxl
```

Используйте `/tasks/batch` для многостраничных партитур.

**Внутренний процесс playlist (2 шага):**

```
Step 1: Создание compound book
        playlist.xml (ссылки на изображения) → playlist.omr

Step 2: Транскрипция и экспорт
        playlist.omr → playlist.mxl
```

## Запуск

```bash
# Через docker-compose
docker compose up -d

# Локально (для разработки)
uvicorn api.main:app --reload
```
