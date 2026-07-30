"""Админка каталога на SQLAdmin (монтируется на /admin).

Логин — одна учётка из env (ADMIN_USERNAME / ADMIN_PASSWORD), сессия в подписанной
cookie (ADMIN_SECRET). slug у всех сущностей генерируется автоматически из
name/title (исключён из форм), поэтому в админке его не вводят.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from sqladmin import Admin, BaseView, ModelView, expose
from sqladmin.ajax import QueryAjaxModelLoader
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import select
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from api.db import SessionLocal

# Кастомные шаблоны форм (переинициализация select2 с minimumInputLength=0).
ADMIN_TEMPLATES_DIR = str(Path(__file__).parent / "admin_templates")


class CatalogAdmin(Admin):
    """Admin с ajax-lookup, допускающим пустой term (для подгрузки первых N в select2)."""

    async def ajax_lookup(self, request: Request) -> JSONResponse:
        identity = request.path_params["identity"]
        model_view = self._find_model_view(identity)

        name = request.query_params.get("name")
        term = request.query_params.get("term") or ""  # пустой term допускаем
        if not name:
            raise HTTPException(status_code=400)
        try:
            loader: QueryAjaxModelLoader = model_view._form_ajax_refs[name]
        except KeyError:
            raise HTTPException(status_code=400)

        data = [loader.format(obj) for obj in await loader.get_list(term)]
        return JSONResponse({"results": data})

from api.catalog_models import (
    AppUser,
    Author,
    Collection,
    CollectionItem,
    Genre,
    Instrument,
    PlayEvent,
    Rating,
    Score,
    ScoreFormat,
    SourceType,
    Style,
    Tag,
    score_genres,
    score_instruments,
    score_tags,
)
from api.catalog_enums import Difficulty
from api.config import settings
from api.stats_models import ProcessingEvent
from api.failures_models import FailedFile
from api.db import engine

from urllib.parse import quote, urlsplit

from markupsafe import Markup

from api.storage import file_public_url


def _media_src(value) -> str | None:
    """Root-relative URL медиа-файла каталога (без хоста и схемы).

    Берём публичный URL из file_public_url и оставляем только путь, чтобы <img>
    в админке наследовал https самой страницы и не ловил mixed-content (медиа
    раздаётся тем же доменом через Caddy /media/*)."""
    url = file_public_url(value)
    if not url:
        return None
    parts = urlsplit(url)
    src = parts.path + (f"?{parts.query}" if parts.query else "")
    return src or url


def _image_formatter(attr_name: str, size: int = 60):
    """Фабрика форматтера колонки: показывает ImageType-поле как <img>, а не
    текстовый путь к файлу. size — высота превью в px."""

    def _fmt(model, attribute):
        src = _media_src(getattr(model, attr_name, None))
        if not src:
            return ""
        return Markup(
            f'<img src="{src}" alt="" loading="lazy" '
            f'style="height:{size}px;width:auto;max-width:{size * 2}px;'
            'object-fit:contain;border-radius:4px;background:#f3f4f6;">'
        )

    return _fmt


# Расширения, которые в архиве провалов имеет смысл показывать превьюшкой.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _failure_src(stored_path: str | None) -> str | None:
    """Root-relative URL сохранённой копии проваленного файла (без хоста/схемы).

    stored_path — абсолютный путь внутри media_root; отдаём путь под /media/*,
    чтобы <img>/<a> в админке наследовали https страницы (mixed-content нет)."""
    if not stored_path:
        return None
    try:
        rel = Path(stored_path).relative_to(Path(settings.media_root))
    except ValueError:
        return None
    prefix = settings.media_path_prefix.strip("/")
    rel_q = quote(rel.as_posix())
    return f"/{prefix}/{rel_q}" if prefix else f"/{rel_q}"


def _failure_file_formatter(model, attribute):
    """Колонка файла в архиве провалов: превью (для картинок) + ссылка «скачать»."""
    src = _failure_src(getattr(model, "stored_path", None))
    if not src:
        return Markup('<span style="color:#9ca3af;">нет файла</span>')
    name = getattr(model, "filename", "") or "файл"
    ext = Path(name).suffix.lower()
    if ext in _IMAGE_EXTS:
        thumb = (
            f'<img src="{src}" alt="" loading="lazy" '
            'style="height:60px;width:auto;max-width:120px;object-fit:contain;'
            'border-radius:4px;background:#f3f4f6;">'
        )
    else:
        thumb = f'<span style="font-family:monospace;">{ext or "?"}</span>'
    return Markup(
        f'<a href="{src}" target="_blank" rel="noopener" '
        f'title="Скачать {name}">{thumb}</a>'
    )


def _error_short_formatter(model, attribute):
    """Короткий превью текста ошибки (полный виден в детальной карточке)."""
    text = getattr(model, "error", None) or ""
    text = text.strip().replace("\n", " ")
    if len(text) > 160:
        text = text[:160] + "…"
    return text or "—"


class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = form.get("username")
        password = form.get("password")
        if username == settings.admin_username and password == settings.admin_password:
            request.session["user"] = str(username)
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return "user" in request.session


# --------------------------------------------------------------------------- #
# Справочники
# --------------------------------------------------------------------------- #
class AuthorAdmin(ModelView, model=Author):
    name = "Автор"
    name_plural = "Авторы"
    category = "Каталог"
    icon = "fa-solid fa-user-pen"
    column_list = [Author.photo, Author.id, Author.name, Author.born, Author.died, Author.source]
    column_searchable_list = [Author.name]
    column_sortable_list = [Author.id, Author.name]
    form_excluded_columns = [Author.slug, Author.scores, Author.created_at]
    # Показываем фото как картинку, а не путь к файлу.
    column_formatters = {Author.photo: _image_formatter("photo")}
    column_formatters_detail = {Author.photo: _image_formatter("photo", size=240)}


class GenreAdmin(ModelView, model=Genre):
    name = "Жанр"
    name_plural = "Жанры"
    category = "Каталог"
    icon = "fa-solid fa-tags"
    column_list = [Genre.id, Genre.name]
    column_searchable_list = [Genre.name]
    form_excluded_columns = [Genre.slug]


class StyleAdmin(ModelView, model=Style):
    name = "Стиль"
    name_plural = "Стили"
    category = "Каталог"
    icon = "fa-solid fa-palette"
    column_list = [Style.id, Style.name]
    column_searchable_list = [Style.name]
    form_excluded_columns = [Style.slug, Style.scores]


class InstrumentAdmin(ModelView, model=Instrument):
    name = "Инструмент"
    name_plural = "Инструменты"
    category = "Каталог"
    icon = "fa-solid fa-guitar"
    column_list = [Instrument.icon, Instrument.id, Instrument.name]
    column_searchable_list = [Instrument.name]
    form_excluded_columns = [Instrument.slug]
    column_formatters = {Instrument.icon: _image_formatter("icon")}
    column_formatters_detail = {
        Instrument.icon: _image_formatter("icon", size=240)
    }


class TagAdmin(ModelView, model=Tag):
    name = "Тег"
    name_plural = "Теги"
    category = "Каталог"
    icon = "fa-solid fa-tag"
    column_list = [Tag.id, Tag.name, Tag.slug]
    column_searchable_list = [Tag.name, Tag.slug]
    column_sortable_list = [Tag.id, Tag.name]
    form_excluded_columns = [Tag.slug, Tag.scores]


# --------------------------------------------------------------------------- #
# Ноты и подборки
# --------------------------------------------------------------------------- #
class ScoreAdmin(ModelView, model=Score):
    name = "Нота"
    name_plural = "Ноты"
    category = "Каталог"
    icon = "fa-solid fa-music"
    list_template = "score_list.html"
    create_template = "ajax_create.html"
    edit_template = "ajax_edit.html"
    column_list = [
        Score.cover,
        Score.id,
        Score.title,
        Score.author,
        Score.style,
        Score.format,
        Score.difficulty,
        Score.rating_avg,
        Score.plays_count,
        Score.is_published,
    ]
    column_searchable_list = [
        Score.title,
        Score.description,
        Score.opus,
        Score.lyricist,
        Score.source_id,
    ]
    # Обложку показываем картинкой, а не путём к файлу.
    column_formatters = {Score.cover: _image_formatter("cover")}
    column_formatters_detail = {Score.cover: _image_formatter("cover", size=320)}
    column_sortable_list = [
        Score.id,
        Score.title,
        Score.rating_avg,
        Score.plays_count,
        Score.created_at,
    ]
    # slug и агрегаты считаются автоматически — в форме не показываем.
    form_excluded_columns = [
        Score.slug,
        Score.rating_avg,
        Score.rating_count,
        Score.plays_count,
        Score.created_at,
        Score.updated_at,
    ]
    # Удобный выбор связей: ajax-поиск (select2) вместо громоздких списков.
    form_ajax_refs = {
        "author": {"fields": ("name",), "order_by": "name"},
        "style": {"fields": ("name",), "order_by": "name"},
        "genres": {"fields": ("name",), "order_by": "name"},
        "instruments": {"fields": ("name",), "order_by": "name"},
        "tags": {"fields": ("name",), "order_by": "name"},
        "collections": {"fields": ("title",), "order_by": "title"},
    }

    @staticmethod
    def _selected_ints(request: Request, name: str) -> list[int]:
        """Корректные целые значения повторяющегося query-параметра."""
        values: list[int] = []
        for raw_value in request.query_params.getlist(name):
            try:
                values.append(int(raw_value))
            except (TypeError, ValueError):
                continue
        return values

    @staticmethod
    def _number(request: Request, name: str, *, integer: bool = False):
        """Числовой query-параметр; невалидное значение просто не фильтрует."""
        raw_value = request.query_params.get(name)
        if raw_value is None or not raw_value.strip():
            return None
        try:
            return int(raw_value) if integer else float(raw_value.replace(",", "."))
        except (TypeError, ValueError):
            return None

    def _apply_filters(self, stmt, request: Request):
        """Применить admin-фильтры и к списку, и к count-запросу."""
        author_ids = self._selected_ints(request, "author")
        if author_ids:
            stmt = stmt.where(Score.author_id.in_(author_ids))

        style_ids = self._selected_ints(request, "style")
        if style_ids:
            stmt = stmt.where(Score.style_id.in_(style_ids))

        genre_ids = self._selected_ints(request, "genre")
        if genre_ids:
            stmt = stmt.where(
                Score.id.in_(
                    select(score_genres.c.score_id).where(
                        score_genres.c.genre_id.in_(genre_ids)
                    )
                )
            )

        instrument_ids = self._selected_ints(request, "instrument")
        if instrument_ids:
            stmt = stmt.where(
                Score.id.in_(
                    select(score_instruments.c.score_id).where(
                        score_instruments.c.instrument_id.in_(instrument_ids)
                    )
                )
            )

        tag_ids = self._selected_ints(request, "tag")
        if tag_ids:
            stmt = stmt.where(
                Score.id.in_(
                    select(score_tags.c.score_id).where(
                        score_tags.c.tag_id.in_(tag_ids)
                    )
                )
            )

        collection_ids = self._selected_ints(request, "collection")
        if collection_ids:
            stmt = stmt.where(
                Score.id.in_(
                    select(CollectionItem.score_id).where(
                        CollectionItem.collection_id.in_(collection_ids)
                    )
                )
            )

        difficulties = []
        for value in self._selected_ints(request, "difficulty"):
            try:
                difficulties.append(Difficulty(value))
            except ValueError:
                continue
        if difficulties:
            stmt = stmt.where(Score.difficulty.in_(difficulties))

        formats = []
        for value in request.query_params.getlist("format"):
            try:
                formats.append(ScoreFormat(value))
            except ValueError:
                continue
        if formats:
            stmt = stmt.where(Score.format.in_(formats))

        sources = []
        for value in request.query_params.getlist("source"):
            try:
                sources.append(SourceType(value))
            except ValueError:
                continue
        if sources:
            stmt = stmt.where(Score.source.in_(sources))

        is_published = request.query_params.get("is_published")
        if is_published in {"true", "false"}:
            stmt = stmt.where(Score.is_published.is_(is_published == "true"))

        has_cover = request.query_params.get("has_cover")
        if has_cover == "true":
            stmt = stmt.where(Score.cover.is_not(None))
        elif has_cover == "false":
            stmt = stmt.where(Score.cover.is_(None))

        range_filters = (
            ("rating_min", Score.rating_avg, ">=", False),
            ("rating_max", Score.rating_avg, "<=", False),
            ("rating_count_min", Score.rating_count, ">=", True),
            ("rating_count_max", Score.rating_count, "<=", True),
            ("plays_min", Score.plays_count, ">=", True),
            ("plays_max", Score.plays_count, "<=", True),
            ("year_from", Score.year, ">=", True),
            ("year_to", Score.year, "<=", True),
        )
        for param, column, operator, integer in range_filters:
            value = self._number(request, param, integer=integer)
            if value is not None:
                stmt = stmt.where(
                    column >= value if operator == ">=" else column <= value
                )

        return stmt

    def list_query(self, request: Request):
        return self._apply_filters(super().list_query(request), request)

    def count_query(self, request: Request):
        return self._apply_filters(super().count_query(request), request)

    def get_filter_options(self) -> dict:
        """Актуальные варианты для фильтров на странице списка нот."""
        with SessionLocal() as db:
            return {
                "authors": db.execute(
                    select(Author.id, Author.name).order_by(Author.name)
                ).all(),
                "genres": db.execute(
                    select(Genre.id, Genre.name).order_by(Genre.name)
                ).all(),
                "styles": db.execute(
                    select(Style.id, Style.name).order_by(Style.name)
                ).all(),
                "instruments": db.execute(
                    select(Instrument.id, Instrument.name).order_by(Instrument.name)
                ).all(),
                "tags": db.execute(
                    select(Tag.id, Tag.name).order_by(Tag.name)
                ).all(),
                "collections": db.execute(
                    select(Collection.id, Collection.title).order_by(Collection.title)
                ).all(),
                "difficulties": list(Difficulty),
                "formats": list(ScoreFormat),
                "sources": list(SourceType),
            }


class CollectionAdmin(ModelView, model=Collection):
    name = "Подборка"
    name_plural = "Подборки"
    category = "Каталог"
    icon = "fa-solid fa-layer-group"
    create_template = "ajax_create.html"
    edit_template = "ajax_edit.html"
    column_list = [
        Collection.cover,
        Collection.id,
        Collection.title,
        Collection.is_featured,
        Collection.is_published,
        Collection.position,
    ]
    column_searchable_list = [Collection.title]
    # Обложку подборки показываем картинкой, а не путём к файлу.
    column_formatters = {Collection.cover: _image_formatter("cover")}
    column_formatters_detail = {Collection.cover: _image_formatter("cover", size=320)}
    column_sortable_list = [Collection.id, Collection.title, Collection.position]
    # source* — служебные поля импорта, в форме не нужны. items — read-only
    # (упорядоченное чтение), ноты добавляются через поле scores ниже.
    form_excluded_columns = [
        Collection.slug,
        Collection.items,
        Collection.created_at,
        Collection.updated_at,
        Collection.source,
        Collection.source_id,
        Collection.source_url,
    ]
    # Инлайн-добавление нот в подборку: множественный выбор с ajax-поиском по названию.
    form_ajax_refs = {
        "scores": {"fields": ("title",), "order_by": "title"}
    }


class CollectionItemAdmin(ModelView, model=CollectionItem):
    name = "Нота в подборке"
    name_plural = "Состав подборок"
    category = "Каталог"
    icon = "fa-solid fa-list-ol"
    column_list = [
        CollectionItem.id,
        CollectionItem.collection,
        CollectionItem.score,
        CollectionItem.position,
    ]
    column_sortable_list = [CollectionItem.id, CollectionItem.position]
    form_columns = [
        CollectionItem.collection,
        CollectionItem.score,
        CollectionItem.position,
    ]


# --------------------------------------------------------------------------- #
# Пользователи и активность (только просмотр)
# --------------------------------------------------------------------------- #
class _ReadOnly(ModelView):
    can_create = False
    can_edit = False
    can_delete = False
    can_view_details = True


class AppUserAdmin(_ReadOnly, model=AppUser):
    name = "Пользователь"
    name_plural = "Пользователи"
    category = "Активность"
    icon = "fa-solid fa-mobile-screen"
    column_list = [AppUser.id, AppUser.device_id, AppUser.created_at]
    column_searchable_list = [AppUser.device_id]


class RatingAdmin(_ReadOnly, model=Rating):
    name = "Оценка"
    name_plural = "Оценки"
    category = "Активность"
    icon = "fa-solid fa-star"
    column_list = [Rating.id, Rating.user_id, Rating.score_id, Rating.value, Rating.created_at]
    column_sortable_list = [Rating.id, Rating.value, Rating.created_at]


class PlayEventAdmin(_ReadOnly, model=PlayEvent):
    name = "Проигрывание"
    name_plural = "Проигрывания"
    category = "Активность"
    icon = "fa-solid fa-play"
    column_list = [PlayEvent.id, PlayEvent.user_id, PlayEvent.score_id, PlayEvent.created_at]
    column_sortable_list = [PlayEvent.id, PlayEvent.created_at]


class ProcessingEventAdmin(_ReadOnly, model=ProcessingEvent):
    name = "Обработка OMR"
    name_plural = "Статистика OMR"
    category = "Активность"
    icon = "fa-solid fa-chart-line"
    column_list = [
        ProcessingEvent.id,
        ProcessingEvent.created_at,
        ProcessingEvent.kind,
        ProcessingEvent.status,
        ProcessingEvent.preset,
        ProcessingEvent.files_total,
        ProcessingEvent.files_completed,
        ProcessingEvent.files_failed,
    ]
    column_searchable_list = [ProcessingEvent.task_id]
    column_sortable_list = [ProcessingEvent.id, ProcessingEvent.created_at, ProcessingEvent.status]
    column_default_sort = ("created_at", True)


class FailedFileAdmin(ModelView, model=FailedFile):
    name = "Проблемный файл"
    name_plural = "Проблемные файлы"
    category = "Активность"
    icon = "fa-solid fa-file-circle-exclamation"
    # Записи создаёт воркер; руками их не заводят. Редактируем только флаг
    # «разобрано», а удаляем — чтобы после аудита выкинуть и строку, и файл.
    can_create = False
    can_edit = True
    can_delete = True
    can_view_details = True
    # stored_path показываем превьюшкой/ссылкой (форматтер), error — коротким
    # текстом; реальные колонки, чтобы SQLAdmin корректно их разрешал.
    column_list = [
        FailedFile.stored_path,
        FailedFile.id,
        FailedFile.created_at,
        FailedFile.kind,
        FailedFile.filename,
        FailedFile.preset,
        FailedFile.enhance,
        FailedFile.reviewed,
        FailedFile.error,
        FailedFile.task_id,
    ]
    column_labels = {
        FailedFile.stored_path: "Файл",
        FailedFile.created_at: "Когда",
        FailedFile.kind: "Тип",
        FailedFile.filename: "Имя файла",
        FailedFile.preset: "Пресет",
        FailedFile.enhance: "enhance",
        FailedFile.reviewed: "Разобрано",
        FailedFile.task_id: "Задача",
        FailedFile.error: "Ошибка",
    }
    column_searchable_list = [FailedFile.task_id, FailedFile.filename]
    column_sortable_list = [
        FailedFile.id,
        FailedFile.created_at,
        FailedFile.kind,
        FailedFile.reviewed,
    ]
    column_default_sort = ("created_at", True)
    # В форме правки — только флаг «разобрано» (остальное иммутабельно).
    form_columns = [FailedFile.reviewed]
    column_formatters = {
        FailedFile.stored_path: _failure_file_formatter,
        FailedFile.error: _error_short_formatter,
    }
    column_formatters_detail = {
        FailedFile.stored_path: _failure_file_formatter,
    }

    async def on_model_delete(self, model, request: Request) -> None:
        """Удаляя строку, убираем и сохранённую копию файла с диска."""
        stored_path = getattr(model, "stored_path", None)
        if stored_path:
            try:
                Path(stored_path).unlink(missing_ok=True)
            except OSError:
                pass


class OmrDashboardView(BaseView):
    name = "Дашборд OMR"
    icon = "fa-solid fa-chart-column"

    @expose("/omr-dashboard", methods=["GET"])
    async def dashboard(self, request: Request):
        try:
            days = max(1, min(int(request.query_params.get("days", 30)), 365))
        except ValueError:
            days = 30

        today = datetime.now(timezone.utc).date()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days - 1)

        db = SessionLocal()
        try:
            rows = db.execute(
                select(
                    ProcessingEvent.created_at,
                    ProcessingEvent.status,
                    ProcessingEvent.files_completed,
                ).where(ProcessingEvent.created_at >= cutoff)
            ).all()
        finally:
            db.close()

        buckets: dict[str, dict[str, int]] = {}
        for created_at, status, files in rows:
            b = buckets.setdefault(
                created_at.date().isoformat(),
                {"tasks": 0, "completed": 0, "failed": 0, "files": 0},
            )
            b["tasks"] += 1
            b["completed" if status == "completed" else "failed"] += 1
            b["files"] += files or 0

        # Непрерывный ряд дат (включая дни без обработок) — для ровного графика.
        series = []
        for i in range(days):
            d = (today - timedelta(days=days - 1 - i)).isoformat()
            b = buckets.get(d, {"tasks": 0, "completed": 0, "failed": 0, "files": 0})
            series.append({"date": d, **b})

        max_tasks = max((s["tasks"] for s in series), default=0) or 1
        totals = {
            "tasks": sum(s["tasks"] for s in series),
            "completed": sum(s["completed"] for s in series),
            "failed": sum(s["failed"] for s in series),
            "files": sum(s["files"] for s in series),
        }
        context = {
            "series": series,
            "max_tasks": max_tasks,
            "totals": totals,
            "days": days,
        }
        return await self.templates.TemplateResponse(request, "omr_dashboard.html", context)


def init_admin(app: FastAPI) -> Admin:
    """Смонтировать админку на /admin и зарегистрировать вьюхи."""
    authentication_backend = AdminAuth(secret_key=settings.admin_secret)
    admin = CatalogAdmin(
        app,
        engine,
        authentication_backend=authentication_backend,
        title="Каталог нот — админка",
        templates_dir=ADMIN_TEMPLATES_DIR,
    )
    for view in (
        ScoreAdmin,
        CollectionAdmin,
        CollectionItemAdmin,
        AuthorAdmin,
        GenreAdmin,
        StyleAdmin,
        InstrumentAdmin,
        TagAdmin,
        AppUserAdmin,
        RatingAdmin,
        PlayEventAdmin,
        ProcessingEventAdmin,
        FailedFileAdmin,
        OmrDashboardView,
    ):
        admin.add_view(view)
    return admin
