# Каталог нот + админка

Аддитивный слой поверх OMR-API: каталог нот (ноты, подборки, авторы, жанры,
стили, инструменты), публичное API для мобильного приложения и веб-админка для
наполнения. OMR-функционал не затронут.

## Архитектура

- **Postgres** (сервис `postgres` в docker-compose) — хранилище каталога.
- **SQLAlchemy 2.x** (синхронный движок) — `api/db.py`, модели `api/catalog_models.py`.
- **Миграции** — Alembic (`api/alembic.ini`, `api/migrations/`). Применяются
  одноразовым сервисом `migrate` в docker-compose (`alembic upgrade head`) до
  старта API; приложение дополнительно гоняет `upgrade head` на старте как
  страховку. Новая ревизия: `alembic -c api/alembic.ini revision --autogenerate -m "..."`.
- **Публичное API** — `api/catalog_routes.py`, префикс `/catalog`, та же авторизация
  по `api_key`, что и у OMR.
- **Админка** — SQLAdmin на `/admin` (`api/admin.py`), логин из env.
- **Файлы** (обложки, mxl/midi/mp3/pdf) кладутся в `CATALOG_MEDIA_DIR`
  (`/storage/out/catalog`) и раздаются существующим `/media/*` (Caddy → `/storage/out`).
  URL строит `api/storage.py:file_public_url`.

## Запуск

```bash
cp env_example .env          # при необходимости поменяйте логины/пароли
docker compose up -d --build
```

- Админка: `http://<host>/admin` (через Caddy) или `:8000/admin` напрямую.
  Логин/пароль — `ADMIN_USERNAME` / `ADMIN_PASSWORD` (по умолчанию `admin`/`admin`,
  **обязательно поменяйте в проде**, как и `ADMIN_SECRET`).

### Переменные окружения

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `POSTGRES_USER/PASSWORD/DB` | креды Postgres | `catalog` |
| `DATABASE_URL` | строка подключения | `postgresql+psycopg2://catalog:catalog@postgres:5432/catalog` |
| `CATALOG_MEDIA_DIR` | куда складывать медиа каталога | `/storage/out/catalog` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | логин в админку | `admin` / `admin` |
| `ADMIN_SECRET` | секрет session-cookie | `change-me-in-production` |

## Модель данных

- **Score** (нота): `title`, `slug` (авто из title), `author`, `style`, `format`
  (solo/ensemble/orchestra/…), `difficulty` (`1`/`2`/`3` в API), `genres[]`, `instruments[]`,
  `opus`, `year`, `lyricist`, `license`, файлы `cover`/`music_file`/`midi_file`/
  `audio_file`/`pdf_file`, агрегаты `rating_avg`/`rating_count`/`plays_count`,
  `is_published`, поля импорта `source`/`source_id`/`source_url`.
- **Collection** (подборка) + **CollectionItem** (упорядоченный состав). Связь
  «нота ↔ подборка» редактируется с обеих сторон: на странице подборки — поле «Ноты»,
  на странице ноты — поле «Подборки» (оба — ajax-поиск select2, первые 10 при фокусе).
  Тонкая настройка порядка (`position`) — во вьюхе «Состав подборок». В API подборка
  читается через упорядоченное `items`.
- Справочники: **Author**, **Genre**, **Style**, **Instrument** (`icon` загружается
  в админке и возвращается в API как `iconUrl`).
- Активность: **AppUser** (по заголовку `X-Device-Id`), **Rating** (1..5,
  unique на пользователя+ноту), **PlayEvent**.

`slug` генерируется автоматически из `name`/`title` и уникализируется (`-2`, `-3`…);
в формах админки скрыт.

## Публичное API (`/catalog`, требует `?api_key=…`)

| Метод | Путь | Описание |
|---|---|---|
| GET | `/catalog/genres` `/styles` `/instruments` `/authors` | справочники |
| GET | `/catalog/collections?featured=true&has_cover=true&page=1&page_size=20` | подборки с фильтрами и пагинацией |
| GET | `/catalog/collections/{id}?page=1&page_size=20` | подборка с нотами и пагинацией |
| GET | `/catalog/scores` | каталог: `q, genre, style, instrument, author, collection, difficulty(1\|2\|3), sort(new\|popular\|rating), page, page_size` |
| GET | `/catalog/scores/popular?period=week\|month` | популярные ноты за последние 7 или 30 дней |
| GET | `/catalog/scores/{slug}` | карточка ноты (ссылки на файлы) |
| POST | `/catalog/scores/{id}/play` | засчитать проигрывание (заголовок `X-Device-Id` опционален) |
| POST | `/catalog/scores/{id}/rate` | оценка `{ "value": 1..5 }` (заголовок `X-Device-Id` обязателен) |

Оценки и проигрывания пересчитывают агрегаты ноты; их показывает админка.

## Импорт корпуса OpenScore Lieder (CC0)

`scripts/import_lieder.py` — идемпотентный импорт (ключ `source=openscore_lieder`
+ `source_id`). В репозитории Lieder рядом с `.mscx` **уже закоммичены готовые `.mxl`**
(`scores/<path>/lc<id>.mxl`), поэтому MuseScore не нужен — `.mxl` качаются по HTTP.

По умолчанию импортёр **также генерирует обложку** (превью первой страницы) из
`.mxl` через verovio → PNG (cairosvg). Требует системную `libcairo2` (уже добавлена
в Dockerfile).

**Фото авторов.** По умолчанию подтягиваются портреты композиторов: из колонки `image`
в `composers.tsv` (прямые ссылки Wikimedia Commons, ~87/107), для остальных — из
Wikidata (свойство P18). Скачанное ужимается до 600px JPEG и сохраняется в `Author.photo`
(в API — `photoUrl`). Отключить: `--no-photos`. В админке фото также можно загрузить/
заменить вручную в карточке автора.

```bash
# По умолчанию: метаданные + .mxl с GitHub + обложка из нот:
docker compose exec audiveris-api python scripts/import_lieder.py --limit 50

# Только метаданные, без .mxl и обложек (быстро):
docker compose exec audiveris-api python scripts/import_lieder.py --limit 50 --no-mxl

# Без генерации обложек (но с .mxl):
docker compose exec audiveris-api python scripts/import_lieder.py --limit 50 --no-cover

# Без фото авторов:
docker compose exec audiveris-api python scripts/import_lieder.py --limit 50 --no-photos

# Из локального клона (медиа с диска; если сами сконвертировали — также .mid/.mp3/.pdf):
git clone https://github.com/OpenScore/Lieder
docker compose exec audiveris-api python scripts/import_lieder.py --repo-dir /path/to/Lieder
```

Скачанные `.mxl` и сгенерированные обложки кладутся в `CATALOG_MEDIA_DIR` и доступны
в карточке ноты как `musicXmlUrl` / `coverUrl`. Повторный запуск не качает и не
перегенерирует уже привязанное.

**Подборки (sets).** Каждый «set» Lieder — группа песен одного композитора (опус/цикл
или свалка «Other songs by …»). **По умолчанию подборки НЕ создаются** — их ведут
вручную в админке. Опционально: `--sets cycles` (только настоящие циклы, ≥2 песни,
~182 из 249) или `--sets all` (все 249). Песни импортируются в любом режиме.

> Обложка рендерится модулем `api/preview.py` (verovio в отдельном процессе —
> он может сегфолтить на кривом MusicXML; cairosvg растеризует SVG в PNG). Этот же
> модуль можно использовать для авто-обложки при ручной загрузке `.mxl` в админке.

## Очистка каталога (для тестов)

`scripts/reset_catalog.py` удаляет **только данные каталога** (БД + медиа в
`CATALOG_MEDIA_DIR`). OMR-задачи в Redis и их файлы не трогает. Без `--yes` —
dry-run (показывает счётчики, ничего не удаляет).

```bash
# посмотреть, что есть:
docker compose exec audiveris-api python scripts/reset_catalog.py

# очистить БД (TRUNCATE + сброс id на Postgres) и удалить медиа:
docker compose exec audiveris-api python scripts/reset_catalog.py --yes

# варианты: --keep-media (оставить файлы), --drop (DROP+CREATE таблиц)
```

`reset_catalog.py` НЕ трогает таблицу статистики OMR (`processing_events`) — она живёт
постоянно.

## Статистика использования OMR-API

Каждая обработка (single/playlist) пишется воркером в таблицу `processing_events`
(`api/stats_models.py`, запись — `api/stats.py:record_processing`, не роняет OMR при
сбоях БД). Поля: `created_at`, `kind`, `preset`, `status` (completed/error),
`files_total/completed/failed`, `enhance`, `analyze`.

- **Метрики по дням**: `GET /stats/daily?days=30` (под `api_key`) — на каждый день
  `tasks` (обработок), `completed`, `failed`, `files` (успешно обработано файлов) +
  итоговые `totalTasks/totalCompleted/totalFailed`.
- **Дашборд в админке**: пункт «Дашборд OMR» (`/admin/omr-dashboard?days=7|30|90`) —
  агрегаты по дням: карточки-итоги, столбиковая диаграмма (зелёное — успешно, красное —
  с ошибкой) и таблица по дням. Кастомная `BaseView` + шаблон `omr_dashboard.html`
  (график на CSS, без внешних библиотек).
- **Список событий**: «Активность» → «Статистика OMR» — сырые события (read-only,
  сортировка по дате, поиск по `task_id`).

> MIDI/MP3/PDF в репозитории нет — только `.mxl`. Если нужны midi/mp3 для
> прослушивания, сконвертируйте локальный клон MuseScore'ом
> (`mscore -j data/corpus_conversion.json` + свои таргеты) и импортируйте с `--repo-dir`.

> Mutopia отдаёт LilyPond/PDF/MIDI без MusicXML — её можно импортировать отдельным
> скриптом как источник PDF/MIDI (не редактируемого MusicXML).
