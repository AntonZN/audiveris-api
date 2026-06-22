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
    Style,
)
from api.config import settings
from api.stats_models import ProcessingEvent
from api.db import engine


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
    column_list = [Author.id, Author.name, Author.born, Author.died, Author.source]
    column_searchable_list = [Author.name]
    column_sortable_list = [Author.id, Author.name]
    form_excluded_columns = [Author.slug, Author.scores, Author.created_at]


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
    column_list = [Instrument.id, Instrument.name]
    column_searchable_list = [Instrument.name]
    form_excluded_columns = [Instrument.slug]


# --------------------------------------------------------------------------- #
# Ноты и подборки
# --------------------------------------------------------------------------- #
class ScoreAdmin(ModelView, model=Score):
    name = "Нота"
    name_plural = "Ноты"
    category = "Каталог"
    icon = "fa-solid fa-music"
    create_template = "ajax_create.html"
    edit_template = "ajax_edit.html"
    column_list = [
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
    column_searchable_list = [Score.title]
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
        "collections": {"fields": ("title",), "order_by": "title"},
    }


class CollectionAdmin(ModelView, model=Collection):
    name = "Подборка"
    name_plural = "Подборки"
    category = "Каталог"
    icon = "fa-solid fa-layer-group"
    create_template = "ajax_create.html"
    edit_template = "ajax_edit.html"
    column_list = [
        Collection.id,
        Collection.title,
        Collection.is_featured,
        Collection.is_published,
        Collection.position,
    ]
    column_searchable_list = [Collection.title]
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
        AppUserAdmin,
        RatingAdmin,
        PlayEventAdmin,
        ProcessingEventAdmin,
        OmrDashboardView,
    ):
        admin.add_view(view)
    return admin
