"""Публичное API каталога для мобильного приложения.

Все эндпоинты — синхронные (`def`), FastAPI исполняет их в threadpool, поэтому
блокирующие обращения к Postgres не стопорят event loop OMR-части.

Авторизация — тем же api_key, что и остальной API. Оценки/проигрывания
привязываются к пользователю приложения по заголовку ``X-Device-Id``.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from api.catalog_enums import Difficulty
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
    Tag,
    score_genres,
    score_instruments,
    score_tags,
)
from api.catalog_schemas import (
    AuthorOut,
    CollectionDetail,
    CollectionListItem,
    CollectionListResponse,
    InstrumentOut,
    PopularInstrumentOut,
    RateRequest,
    ScoreDetail,
    ScoreListItem,
    ScoreListResponse,
    ScoreStats,
    TagListResponse,
    TermOut,
)
from api.db import get_db
from api.deps import get_api_key
from api.storage import file_public_url

router = APIRouter(
    prefix="/catalog",
    tags=["Catalog"],
    dependencies=[Depends(get_api_key)],
)


# --------------------------------------------------------------------------- #
# Мапперы ORM -> схемы
# --------------------------------------------------------------------------- #
def _term(obj) -> TermOut:
    return TermOut(id=obj.id, name=obj.name, slug=obj.slug)


def _instrument_out(i: Instrument) -> InstrumentOut:
    return InstrumentOut(
        id=i.id,
        name=i.name,
        slug=i.slug,
        icon_url=file_public_url(i.icon),
    )


def _author_out(a: Author) -> AuthorOut:
    return AuthorOut(
        id=a.id,
        name=a.name,
        slug=a.slug,
        bio=a.bio,
        photo_url=file_public_url(a.photo),
        born=a.born,
        died=a.died,
    )


def _score_item(s: Score) -> ScoreListItem:
    return ScoreListItem(
        id=s.id,
        title=s.title,
        slug=s.slug,
        author=s.author.name if s.author else None,
        instruments=[_instrument_out(i) for i in s.instruments],
        tags=[_term(tag) for tag in s.tags],
        cover_url=file_public_url(s.cover),
        audio_url=file_public_url(s.audio_file),
        format=s.format.value if s.format else None,
        difficulty=s.difficulty.value if s.difficulty else None,
        style=s.style.name if s.style else None,
        rating_avg=round(s.rating_avg or 0.0, 2),
        rating_count=s.rating_count or 0,
        plays_count=s.plays_count or 0,
    )


def _score_detail(s: Score) -> ScoreDetail:
    base = _score_item(s).model_dump(by_alias=False)
    return ScoreDetail(
        **base,
        description=s.description,
        author_obj=_author_out(s.author) if s.author else None,
        genres=[_term(g) for g in s.genres],
        opus=s.opus,
        year=s.year,
        lyricist=s.lyricist,
        license=s.license,
        license_url=s.license_url,
        source=s.source.value if s.source else None,
        source_id=s.source_id,
        source_url=s.source_url,
        imslp_url=s.imslp_url,
        music_xml_url=file_public_url(s.music_file),
        midi_url=file_public_url(s.midi_file),
        pdf_url=file_public_url(s.pdf_file),
        created_at=s.created_at,
    )


# --------------------------------------------------------------------------- #
# Справочники
# --------------------------------------------------------------------------- #
@router.get("/genres", response_model=list[TermOut], summary="Список жанров")
def list_genres(db: Session = Depends(get_db)) -> list[TermOut]:
    """Все жанры (id/name/slug), по алфавиту. Для фильтров каталога используйте
    `slug` в `GET /catalog/scores?genre=<slug>`. Удобно для чипов/выпадашек."""
    rows = db.execute(select(Genre).order_by(Genre.name)).scalars().all()
    return [_term(r) for r in rows]


@router.get("/styles", response_model=list[TermOut], summary="Список стилей")
def list_styles(db: Session = Depends(get_db)) -> list[TermOut]:
    """Все стили (id/name/slug). `slug` → фильтр `GET /catalog/scores?style=<slug>`."""
    rows = db.execute(select(Style).order_by(Style.name)).scalars().all()
    return [_term(r) for r in rows]


@router.get(
    "/instruments",
    response_model=list[InstrumentOut],
    summary="Список инструментов",
)
def list_instruments(db: Session = Depends(get_db)) -> list[InstrumentOut]:
    """Все инструменты (`id`, `name`, `slug`, `iconUrl`). `slug` используется
    в фильтре `?instrument=<slug>`; `iconUrl` может быть null."""
    rows = db.execute(select(Instrument).order_by(Instrument.name)).scalars().all()
    return [_instrument_out(r) for r in rows]


@router.get(
    "/instruments/popular",
    response_model=list[PopularInstrumentOut],
    summary="Популярные инструменты с партитурами",
)
def list_popular_instruments(
    limit: int = Query(
        6,
        ge=1,
        le=20,
        description="Сколько инструментов вернуть, 1..20 (по умолчанию 6)",
    ),
    db: Session = Depends(get_db),
) -> list[PopularInstrumentOut]:
    """Только инструменты, у которых есть хотя бы одна опубликованная
    партитура. Сортировка по числу партитур по убыванию, затем по названию.

    `scoresCount` можно показать в интерфейсе или использовать для аналитики.
    Для фильтра каталога передавайте `slug` в
    `GET /catalog/scores?instrument=<slug>`.
    """
    scores_count = func.count(Score.id).label("scores_count")
    rows = db.execute(
        select(Instrument, scores_count)
        .join(
            score_instruments,
            score_instruments.c.instrument_id == Instrument.id,
        )
        .join(Score, Score.id == score_instruments.c.score_id)
        .where(Score.is_published.is_(True))
        .group_by(Instrument.id)
        .order_by(scores_count.desc(), Instrument.name, Instrument.id)
        .limit(limit)
    ).all()

    return [
        PopularInstrumentOut(
            id=instrument.id,
            name=instrument.name,
            slug=instrument.slug,
            icon_url=file_public_url(instrument.icon),
            scores_count=count,
        )
        for instrument, count in rows
    ]


@router.get("/authors", response_model=list[AuthorOut], summary="Список авторов")
def list_authors(db: Session = Depends(get_db)) -> list[AuthorOut]:
    """Все авторы с фото и годами жизни. `slug` → фильтр `?author=<slug>`;
    `photoUrl` — абсолютная ссылка на портрет (может быть null)."""
    rows = db.execute(select(Author).order_by(Author.name)).scalars().all()
    return [_author_out(r) for r in rows]


@router.get(
    "/tags",
    response_model=TagListResponse,
    summary="Список тегов",
)
def list_tags(
    page: int = Query(1, ge=1, description="Номер страницы, с 1"),
    page_size: int = Query(
        20,
        ge=1,
        le=100,
        description="Размер страницы, 1..100 (по умолчанию 20)",
    ),
    db: Session = Depends(get_db),
) -> TagListResponse:
    """Теги для нот, по алфавиту. Ответ использует пагинацию
    `{ items, total, page, pageSize }`. Поле `slug` передавайте повторяющимся
    параметром `tag` в `GET /catalog/scores`, например
    `?tag=for-beginners&tag=popular`."""
    total = db.execute(select(func.count(Tag.id))).scalar_one()
    rows = db.execute(
        select(Tag)
        .order_by(Tag.name, Tag.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()
    return TagListResponse(
        items=[_term(tag) for tag in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# --------------------------------------------------------------------------- #
# Подборки
# --------------------------------------------------------------------------- #
@router.get(
    "/collections",
    response_model=CollectionListResponse,
    summary="Список подборок",
)
def list_collections(
    featured: bool | None = Query(None, description="Только избранные (для главной)"),
    has_cover: bool | None = Query(
        None,
        description="true — только с обложкой, false — только без обложки",
    ),
    page: int = Query(1, ge=1, description="Номер страницы, с 1"),
    page_size: int = Query(
        20,
        ge=1,
        le=100,
        description="Размер страницы, 1..100 (по умолчанию 20)",
    ),
    db: Session = Depends(get_db),
) -> CollectionListResponse:
    """Опубликованные подборки (кураторские списки нот), в порядке `position`.
    `?featured=true` — только избранные, `?has_cover=true` — только подборки с
    обложкой. В счётчик состава входят только опубликованные ноты с обложкой и
    MusicXML/MXL. Ответ использует пагинацию `{ items, total, page, pageSize }`;
    за составом идите в `GET /catalog/collections/{id}`."""
    stmt = select(Collection).where(Collection.is_published.is_(True))
    if featured is not None:
        stmt = stmt.where(Collection.is_featured.is_(featured))
    if has_cover is not None:
        stmt = stmt.where(
            Collection.cover.is_not(None) if has_cover else Collection.cover.is_(None)
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()

    stmt = (
        stmt.order_by(Collection.position, Collection.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = db.execute(stmt).scalars().all()

    # Считаем только те ноты, которые реально можно показать в подборке.
    # Иначе `items_count` не совпадал бы с составом detail-endpoint.
    items_counts: dict[int, int] = {}
    if rows:
        items_counts = dict(
            db.execute(
                select(CollectionItem.collection_id, func.count(Score.id))
                .join(Score, Score.id == CollectionItem.score_id)
                .where(
                    CollectionItem.collection_id.in_([c.id for c in rows]),
                    Score.is_published.is_(True),
                    Score.cover.is_not(None),
                    Score.music_file.is_not(None),
                )
                .group_by(CollectionItem.collection_id)
            ).all()
        )

    return CollectionListResponse(
        items=[
            CollectionListItem(
                id=c.id,
                title=c.title,
                slug=c.slug,
                description=c.description,
                cover_url=file_public_url(c.cover),
                is_featured=c.is_featured,
                items_count=items_counts.get(c.id, 0),
            )
            for c in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/collections/{collection_id}",
    response_model=CollectionDetail,
    summary="Подборка с нотами (по id)",
)
def get_collection(
    collection_id: int,
    page: int = Query(1, ge=1, description="Номер страницы, с 1"),
    page_size: int = Query(
        20,
        ge=1,
        le=100,
        description="Размер страницы, 1..100 (по умолчанию 20)",
    ),
    db: Session = Depends(get_db),
) -> CollectionDetail:
    """Подборка по `id` вместе с её нотами (`scores`, краткие карточки) в
    заданном порядке. Поддерживает пагинацию через `page` и `page_size`;
    ответ содержит `total`, `page`, `pageSize`. Только опубликованные ноты.
    404, если подборки нет."""
    c = db.execute(
        select(Collection)
        .where(Collection.id == collection_id, Collection.is_published.is_(True))
    ).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Неполные ноты не должны попадать в публичный состав подборки: карточке
    # нужны и обложка, и доступный MusicXML/MXL для открытия партитуры.
    score_filters = (
        CollectionItem.collection_id == c.id,
        Score.is_published.is_(True),
        Score.cover.is_not(None),
        Score.music_file.is_not(None),
    )
    total = db.execute(
        select(func.count())
        .select_from(CollectionItem)
        .join(Score, Score.id == CollectionItem.score_id)
        .where(*score_filters)
    ).scalar_one()

    scores = db.execute(
        select(Score)
        .join(CollectionItem, CollectionItem.score_id == Score.id)
        .where(*score_filters)
        .order_by(CollectionItem.position, CollectionItem.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .options(
            selectinload(Score.author),
            selectinload(Score.style),
            selectinload(Score.instruments),
            selectinload(Score.tags),
        )
    ).scalars().all()

    return CollectionDetail(
        id=c.id,
        title=c.title,
        slug=c.slug,
        description=c.description,
        cover_url=file_public_url(c.cover),
        is_featured=c.is_featured,
        items_count=total,
        scores=[_score_item(s) for s in scores],
        total=total,
        page=page,
        page_size=page_size,
    )


# --------------------------------------------------------------------------- #
# Ноты: список с фильтрами/поиском/сортировкой
# --------------------------------------------------------------------------- #
_SORTS = {
    "new": (Score.created_at.desc(),),
    "popular": (Score.plays_count.desc(), Score.created_at.desc()),
    "rating": (Score.rating_avg.desc(), Score.rating_count.desc()),
}


@router.get("/scores", response_model=ScoreListResponse, summary="Каталог нот")
def list_scores(
    q: str | None = Query(None, description="Поиск по подстроке названия (регистронезависимо)"),
    genre: str | None = Query(None, description="Фильтр по жанру — slug из GET /catalog/genres"),
    style: str | None = Query(None, description="Фильтр по стилю — slug из GET /catalog/styles"),
    instrument: str | None = Query(
        None, description="Фильтр по инструменту — slug из GET /catalog/instruments"
    ),
    author: str | None = Query(None, description="Фильтр по автору — slug из GET /catalog/authors"),
    tag: list[str] | None = Query(
        None,
        description=(
            "Фильтр по тегам — повторяющийся slug из GET /catalog/tags; "
            "при нескольких значениях подходит любой из тегов"
        ),
    ),
    collection: int | None = Query(
        None,
        ge=1,
        description="Фильтр по подборке — id из GET /catalog/collections",
    ),
    difficulty: Difficulty | None = Query(
        None,
        description="Фильтр по сложности: 1, 2 или 3",
    ),
    sort: str = Query(
        "new",
        pattern="^(new|popular|rating)$",
        description="Сортировка: new — новые (по умолчанию), popular — по проигрываниям, rating — по среднему рейтингу",
    ),
    page: int = Query(1, ge=1, description="Номер страницы, с 1"),
    page_size: int = Query(20, ge=1, le=100, description="Размер страницы, 1..100 (по умолчанию 20)"),
    db: Session = Depends(get_db),
) -> ScoreListResponse:
    """Главный метод каталога: опубликованные ноты с пагинацией, фильтрами,
    поиском и сортировкой.

    **Фильтры** (`q`, `genre`, `style`, `instrument`, `author`, `tag`,
    `collection`, `difficulty`) **необязательны и комбинируются по И** — например
    `?genre=lied&collection=3&difficulty=2&sort=popular` вернёт ноты сложности 2
    в жанре Lied из подборки с id=3, по популярности. `genre`, `style`,
    `instrument` и `author` принимают `slug` из соответствующих справочников;
    `tag` принимает `slug` из `GET /catalog/tags` и может повторяться
    (`?tag=popular&tag=for-beginners` — подходит любой из указанных тегов);
    `collection` принимает `id` из `GET /catalog/collections`, а `difficulty` —
    одно из значений `1`, `2`, `3`. `q` ищет вхождение в названии.

    **Пагинация.** Ответ: `{ items, total, page, pageSize }`. Всего страниц =
    `ceil(total / pageSize)`; следующая есть, пока `page * pageSize < total`.

    **Поля `items` — краткие** (обложка, автор-строка, инструменты, формат,
    сложность, стиль, агрегаты рейтинга/проигрываний). За полной карточкой,
    файлами (MusicXML/MIDI/MP3/PDF) и списком жанров идите в
    `GET /catalog/scores/{id}` по полю `id` из элемента списка.

    **Формат ответа** — camelCase (`coverUrl`, `ratingAvg`, `pageSize` …);
    `*Url`-поля — абсолютные ссылки на медиа (можно подставлять в `src`).
    """
    stmt = select(Score).where(Score.is_published.is_(True))

    if q:
        stmt = stmt.where(Score.title.ilike(f"%{q}%"))
    if difficulty is not None:
        stmt = stmt.where(Score.difficulty == difficulty)
    if style:
        stmt = stmt.join(Score.style).where(Style.slug == style)
    if author:
        stmt = stmt.join(Score.author).where(Author.slug == author)
    tag_slugs = {
        slug.strip()
        for value in tag or []
        for slug in value.split(",")
        if slug.strip()
    }
    if tag_slugs:
        stmt = stmt.where(
            Score.id.in_(
                select(score_tags.c.score_id)
                .join(Tag, Tag.id == score_tags.c.tag_id)
                .where(Tag.slug.in_(sorted(tag_slugs)))
            )
        )
    if genre:
        stmt = stmt.where(
            Score.id.in_(
                select(score_genres.c.score_id)
                .join(Genre, Genre.id == score_genres.c.genre_id)
                .where(Genre.slug == genre)
            )
        )
    if instrument:
        stmt = stmt.where(
            Score.id.in_(
                select(score_instruments.c.score_id)
                .join(Instrument, Instrument.id == score_instruments.c.instrument_id)
                .where(Instrument.slug == instrument)
            )
        )
    if collection:
        stmt = stmt.where(
            Score.id.in_(
                select(CollectionItem.score_id)
                .join(Collection, Collection.id == CollectionItem.collection_id)
                .where(
                    CollectionItem.collection_id == collection,
                    Collection.is_published.is_(True),
                )
            )
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()

    stmt = stmt.order_by(*_SORTS[sort]).offset((page - 1) * page_size).limit(page_size)
    rows = db.execute(
        stmt.options(
            selectinload(Score.author),
            selectinload(Score.style),
            selectinload(Score.instruments),
            selectinload(Score.tags),
        )
    ).scalars().all()

    return ScoreListResponse(
        items=[_score_item(s) for s in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/scores/popular",
    response_model=ScoreListResponse,
    summary="Популярные ноты за неделю или месяц",
)
def list_popular_scores(
    period: str = Query(
        "week",
        pattern="^(week|month)$",
        description="Период популярности: week — последние 7 дней, month — последние 30 дней",
    ),
    page: int = Query(1, ge=1, description="Номер страницы, с 1"),
    page_size: int = Query(
        20,
        ge=1,
        le=100,
        description="Размер страницы, 1..100 (по умолчанию 20)",
    ),
    db: Session = Depends(get_db),
) -> ScoreListResponse:
    """Опубликованные ноты, отсортированные по числу проигрываний за выбранный
    период. `week` считает последние 7 дней, `month` — последние 30 дней.
    Ноты без проигрываний за период в ответ не попадают.

    Ответ использует стандартную пагинацию `{ items, total, page, pageSize }`.
    """
    period_days = 7 if period == "week" else 30
    cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)

    period_plays = (
        select(
            PlayEvent.score_id.label("score_id"),
            func.count(PlayEvent.id).label("period_plays_count"),
        )
        .where(PlayEvent.created_at >= cutoff)
        .group_by(PlayEvent.score_id)
        .subquery()
    )
    stmt = (
        select(Score)
        .join(period_plays, period_plays.c.score_id == Score.id)
        .where(Score.is_published.is_(True))
    )
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()

    rows = db.execute(
        stmt.order_by(
            period_plays.c.period_plays_count.desc(),
            Score.plays_count.desc(),
            Score.created_at.desc(),
            Score.id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
        .options(
            selectinload(Score.author),
            selectinload(Score.style),
            selectinload(Score.instruments),
            selectinload(Score.tags),
        )
    ).scalars().all()

    return ScoreListResponse(
        items=[_score_item(s) for s in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/scores/{score_id}", response_model=ScoreDetail, summary="Карточка ноты (по id)")
def get_score(score_id: int, db: Session = Depends(get_db)) -> ScoreDetail:
    """Полная карточка ноты по `id`: описание, объект автора (`authorObj`),
    жанры/инструменты и ссылки на файлы — `musicXmlUrl`, `midiUrl`, `audioUrl`,
    `pdfUrl` (любая может быть null, если файл не привязан). 404, если нет ноты.

    `id` берётся из поля `id` элемента списка `GET /catalog/scores`. Это «экран
    ноты»: тут лежат медиа для плеера/просмотра, в отличие от краткой карточки
    списка."""
    s = db.execute(
        select(Score)
        .where(Score.id == score_id, Score.is_published.is_(True))
        .options(
            selectinload(Score.author),
            selectinload(Score.style),
            selectinload(Score.genres),
            selectinload(Score.instruments),
            selectinload(Score.tags),
        )
    ).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Score not found")
    return _score_detail(s)


# --------------------------------------------------------------------------- #
# Активность: проигрывание и оценка
# --------------------------------------------------------------------------- #
def _get_or_create_user(db: Session, device_id: str) -> AppUser:
    user = db.execute(
        select(AppUser).where(AppUser.device_id == device_id)
    ).scalar_one_or_none()
    if user is None:
        user = AppUser(device_id=device_id)
        db.add(user)
        db.flush()
    return user


def _stats(db: Session, score: Score, my_rating: int | None = None) -> ScoreStats:
    return ScoreStats(
        score_id=score.id,
        rating_avg=round(score.rating_avg or 0.0, 2),
        rating_count=score.rating_count or 0,
        plays_count=score.plays_count or 0,
        my_rating=my_rating,
    )


@router.post(
    "/scores/{score_id}/play",
    response_model=ScoreStats,
    summary="Засчитать проигрывание",
)
def register_play(
    score_id: int,
    db: Session = Depends(get_db),
    device_id: str | None = Header(None, alias="X-Device-Id"),
) -> ScoreStats:
    """Засчитать одно проигрывание ноты (инкремент `playsCount`). Вызывайте при
    старте воспроизведения. Заголовок `X-Device-Id` опционален: если передан —
    проигрывание привязывается к пользователю устройства. Возвращает свежие
    агрегаты (`ScoreStats`). Принимает числовой `score_id` (не slug)."""
    score = db.get(Score, score_id)
    if not score:
        raise HTTPException(status_code=404, detail="Score not found")

    user = _get_or_create_user(db, device_id) if device_id else None
    db.add(PlayEvent(score_id=score.id, user_id=user.id if user else None))
    score.plays_count = (score.plays_count or 0) + 1
    db.commit()
    return _stats(db, score)


@router.post(
    "/scores/{score_id}/rate",
    response_model=ScoreStats,
    summary="Поставить оценку (1..5)",
)
def rate_score(
    score_id: int,
    payload: RateRequest,
    db: Session = Depends(get_db),
    device_id: str | None = Header(None, alias="X-Device-Id"),
) -> ScoreStats:
    """Поставить/изменить оценку ноты (1..5). Тело: `{ "value": 1..5 }`.
    Заголовок `X-Device-Id` **обязателен** (иначе 400) — оценка одна на
    устройство, повторный вызов перезаписывает прежнюю. В ответе пересчитанные
    `ratingAvg`/`ratingCount` и `myRating`. Принимает числовой `score_id`."""
    if not device_id:
        raise HTTPException(status_code=400, detail="X-Device-Id header is required")

    score = db.get(Score, score_id)
    if not score:
        raise HTTPException(status_code=404, detail="Score not found")

    user = _get_or_create_user(db, device_id)
    rating = db.execute(
        select(Rating).where(Rating.user_id == user.id, Rating.score_id == score.id)
    ).scalar_one_or_none()
    if rating is None:
        rating = Rating(user_id=user.id, score_id=score.id, value=payload.value)
        db.add(rating)
    else:
        rating.value = payload.value
    db.flush()

    avg, cnt = db.execute(
        select(func.avg(Rating.value), func.count(Rating.id)).where(Rating.score_id == score.id)
    ).one()
    score.rating_avg = float(avg or 0.0)
    score.rating_count = int(cnt or 0)
    db.commit()
    return _stats(db, score, my_rating=payload.value)
