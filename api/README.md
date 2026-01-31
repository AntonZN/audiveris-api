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
| `services.py` | Бизнес-логика Audiveris + предобработка изображений |
| `worker.py` | Фоновый обработчик очереди |
| `deps.py` | Зависимости FastAPI (авторизация) |
| `cleanup.py` | Очистка старых задач |
| `exceptions.py` | Кастомные исключения |

## Авторизация

Все эндпоинты (кроме `/health`) требуют API токен в заголовке:

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8000/tasks/...
```

Токен задаётся через переменную окружения `API_TOKEN` (по умолчанию: `123`).

## API Endpoints

### POST /tasks/single

Создание задачи на распознавание **одного файла**.

**Поддерживаемые форматы:**
- PNG, JPG — изображения
- PDF — до 5 страниц (настраивается через `MAX_PDF_PAGES`)

**Валидация:**
- Формат файла определяется по magic bytes (не по расширению)
- PDF проверяется на количество страниц
- Изображения автоматически улучшаются перед обработкой

**Request:**
- `file` (multipart/form-data) — файл изображения или PDF

**Response:**
```json
{
  "taskId": "abc123def456",
  "status": "queued"
}
```

**Пример:**

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "file=@score.png" \
  http://localhost:8000/tasks/single
```

**Ошибки:**
- `400` — Неподдерживаемый формат файла
- `400` — PDF содержит более 5 страниц

---

### POST /tasks/playlist

Создание задачи на распознавание **нескольких файлов** как единого произведения (плейлист).

Все файлы объединяются в один book → на выходе один MusicXML.

**Request:**
- `files` (multipart/form-data) — несколько файлов изображений (PNG, JPG)

**Response:**
```json
{
  "taskId": "abc123def456",
  "status": "queued"
}
```

**Пример:**

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "files=@page1.png" \
  -F "files=@page2.png" \
  -F "files=@page3.png" \
  http://localhost:8000/tasks/playlist
```

---

### GET /tasks/{task_id}

Получение статуса и деталей задачи.

**Response:**
```json
{
  "id": "abc123def456",
  "status": "completed",
  "createdAt": "2024-01-15T10:30:00Z",
  "updatedAt": "2024-01-15T10:30:05Z",
  "progress": {
    "total": 1,
    "completed": 1,
    "failed": 0
  },
  "results": {
    "filename": "score.mxl",
    "url": "http://localhost:8081/out/abc123/score.mxl",
    "logUrl": "http://localhost:8081/out/abc123/score.log"
  },
  "errors": null
}
```

**Статусы задачи:**

| Статус | Описание | Действие клиента |
|--------|----------|------------------|
| `queued` | В очереди на обработку | Повторить запрос через 5-10 сек |
| `running` | Обрабатывается | Повторить запрос через 5-10 сек |
| `completed` | Успешно завершена | Забрать результат из `results` |
| `error` | Завершена с ошибкой | Показать ошибку из `errors` |

**Пример:**
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8000/tasks/abc123def456
```

---

### GET /health

Health check (не требует авторизации).

**Response:**
```json
{
  "status": "ok",
  "queueDepth": 5
}
```

## Предобработка изображений

Перед отправкой в Audiveris изображения автоматически улучшаются:

1. **Upscale** — если ширина или высота < 2000px, изображение увеличивается в 2 раза
2. **Контраст** — повышается на 20%
3. **Резкость** — повышается на 50%

Это помогает Audiveris лучше распознавать ноты на изображениях низкого качества.

**Настройки предобработки:**

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `IMAGE_MIN_DIMENSION` | `2000` | Порог для upscale (px) |
| `IMAGE_UPSCALE_FACTOR` | `2.0` | Множитель увеличения |
| `IMAGE_CONTRAST_FACTOR` | `1.2` | Коэффициент контраста |
| `IMAGE_SHARPNESS_FACTOR` | `1.5` | Коэффициент резкости |

## Переменные окружения

### Основные

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `API_TOKEN` | `123` | Токен для авторизации |
| `AUDIVERIS_CMD` | `audiveris` | Путь к исполняемому файлу |
| `INPUT_DIR` | `storage/in` | Директория для входных файлов |
| `OUTPUT_DIR` | `storage/out` | Директория для результатов |
| `REDIS_URL` | `redis://redis:6379/0` | URL подключения к Redis |
| `TASK_WORKERS` | `1` | Количество воркеров |

### Валидация

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `MIN_INTERLINE` | `11` | Минимальный interline (px) |
| `MAX_PDF_PAGES` | `5` | Максимум страниц в PDF |

### Media URLs

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `MEDIA_ROOT` | `/storage` | Корень для построения URL |
| `MEDIA_BASE_URL` | `http://localhost:8081` | Базовый URL для файлов |
| `MEDIA_PATH_PREFIX` | `` | Префикс пути в URL |

### Очистка

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `TASK_TTL_SECONDS` | `86400` | TTL задачи (24 часа) |
| `CLEANUP_INTERVAL_SECONDS` | `3600` | Интервал очистки (1 час) |

## Обработка ошибок

### low_interline

Разрешение изображения слишком низкое (даже после предобработки).

```json
{
  "filename": "score.png",
  "error": "Image resolution too low: interline=8px < 11px",
  "logUrl": "http://localhost:8081/out/abc123/score.log"
}
```

### processing_failed

Общая ошибка обработки Audiveris.

```json
{
  "filename": "score.png",
  "error": "Audiveris failed. No sheet found",
  "logUrl": "http://localhost:8081/out/abc123/score.log"
}
```

## Режимы обработки

### /tasks/single — Один файл

Один файл → один MusicXML.

```
file: score.png
      ↓
[preprocessing: upscale + enhance]
      ↓
result: score.mxl
```

### /tasks/playlist — Плейлист (несколько файлов)

Все файлы объединяются в один book → один MusicXML.

```
files: [page1.png, page2.png, page3.png]
      ↓
[preprocessing: upscale + enhance each]
      ↓
result: playlist.mxl
```

**Внутренний процесс playlist (2 шага):**

```
Step 1: Создание compound book
        playlist.xml (ссылки на изображения) → playlist.omr

Step 2: Транскрипция и экспорт
        playlist.omr → playlist.mxl
```

## Тестирование предобработки

Для локального тестирования улучшения изображений:

```bash
# Базовый тест
python test_image_preprocess.py image.png

# С кастомными параметрами
python test_image_preprocess.py image.png --factor 3.0 --contrast 1.5 --sharpness 2.0

# Указать output
python test_image_preprocess.py image.png -o result.png
```

## Запуск

```bash
# Через docker-compose
docker compose up -d

# Локально (для разработки)
pip install -r api/requirements.txt
uvicorn api.main:app --reload
```

## Зависимости

- **fastapi** — веб-фреймворк
- **redis** — клиент Redis
- **pydantic-settings** — конфигурация
- **pypdf** — работа с PDF (подсчёт страниц)
- **pillow** — предобработка изображений

---

## TODO: Улучшение предобработки изображений

Текущая предобработка использует Pillow с LANCZOS-интерполяцией. Для более качественного upscale можно рассмотреть AI-решения:

### Требования Audiveris к изображениям

Из документации Audiveris:

| Параметр | Рекомендация |
|----------|--------------|
| DPI | 300 (оптимально), 400 для мелких символов |
| Interline | ~20 пикселей между линиями нотного стана |
| Формат | Grayscale предпочтительнее |

**Interline** — ключевой параметр. Если < 11px, Audiveris отклоняет изображение.

### Варианты AI-upscale

#### 1. waifu2x-ncnn-py

```bash
pip install waifu2x-ncnn-py
```

```python
from waifu2x_ncnn_py import Waifu2x
waifu2x = Waifu2x(gpuid=0, scale=2, noise=1)
output = waifu2x.process(input_image)
```

- Качественный upscale 2x-4x
- Встроенный denoise
- Требует GPU для скорости

#### 2. Real-ESRGAN

```bash
pip install realesrgan
```

- Современная модель super-resolution
- Тянет PyTorch (~2GB)
- Отличное качество, но тяжёлый

#### 3. OpenCV DNN Super Resolution

```python
import cv2
sr = cv2.dnn_superres.DnnSuperResImpl_create()
sr.readModel("EDSR_x4.pb")
sr.setModel("edsr", 4)
result = sr.upsample(image)
```

- Нужно скачать модели отдельно
- Работает на CPU
- Средняя скорость

### Сравнение подходов

| Метод | Качество | Скорость | Зависимости |
|-------|----------|----------|-------------|
| LANCZOS (текущий) | Хорошее | Быстро | Pillow |
| waifu2x | Отличное | Средне (GPU) | ~100MB модели |
| Real-ESRGAN | Отличное | Медленно | PyTorch ~2GB |
| OpenCV DNN | Хорошее | Средне | ~50MB модели |

### Возможные улучшения

1. **Адаптивный upscale** — если interline < 10px, использовать 4x вместо 2x
2. **AI-upscale как опция** — флаг `IMAGE_USE_AI_UPSCALE=true`
3. **Автоопределение качества** — анализировать изображение и выбирать метод
4. **Кэширование** — не обрабатывать повторно одинаковые изображения
