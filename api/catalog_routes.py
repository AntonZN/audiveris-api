"""Публичное API каталога для мобильного приложения.

Все эндпоинты — синхронные (`def`), FastAPI исполняет их в threadpool, поэтому
блокирующие обращения к Postgres не стопорят event loop OMR-части.

Авторизация — тем же api_key, что и остальной API. Оценки/проигрывания
привязываются к пользователю приложения по заголовку ``X-Device-Id``.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from api.catalog_models import (
    AppUser,
    Author,
    Collection,
    Genre,
    Instrument,
    PlayEvent,
    Rating,
    Score,
    Style,
    score_genres,
    score_instruments,
)
from api.catalog_schemas import (
    AuthorOut,
    CollectionDetail,
    CollectionListItem,
    RateRequest,
    ScoreDetail,
    ScoreListItem,
    ScoreListResponse,
    ScoreStats,
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
        cover_url=file_public_url(s.cover),
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
        instruments=[_term(i) for i in s.instruments],
        opus=s.opus,
        year=s.year,
        lyricist=s.lyricist,
        license=s.license,
        imslp_url=s.imslp_url,
        music_xml_url=file_public_url(s.music_file),
        midi_url=file_public_url(s.midi_file),
        audio_url=file_public_url(s.audio_file),
        pdf_url=file_public_url(s.pdf_file),
        created_at=s.created_at,
    )


# --------------------------------------------------------------------------- #
# Справочники
# --------------------------------------------------------------------------- #
@router.get("/genres", response_model=list[TermOut], summary="Список жанров")
def list_genres(db: Session = Depends(get_db)) -> list[TermOut]:
    rows = db.execute(select(Genre).order_by(Genre.name)).scalars().all()
    return [_term(r) for r in rows]


@router.get("/styles", response_model=list[TermOut], summary="Список стилей")
def list_styles(db: Session = Depends(get_db)) -> list[TermOut]:
    rows = db.execute(select(Style).order_by(Style.name)).scalars().all()
    return [_term(r) for r in rows]


@router.get("/instruments", response_model=list[TermOut], summary="Список инструментов")
def list_instruments(db: Session = Depends(get_db)) -> list[TermOut]:
    rows = db.execute(select(Instrument).order_by(Instrument.name)).scalars().all()
    return [_term(r) for r in rows]


@router.get("/authors", response_model=list[AuthorOut], summary="Список авторов")
def list_authors(db: Session = Depends(get_db)) -> list[AuthorOut]:
    rows = db.execute(select(Author).order_by(Author.name)).scalars().all()
    return [_author_out(r) for r in rows]


# --------------------------------------------------------------------------- #
# Подборки
# --------------------------------------------------------------------------- #
@router.get("/collections", response_model=list[CollectionListItem], summary="Список подборок")
def list_collections(
    featured: bool | None = Query(None, description="Только избранные (для главной)"),
    db: Session = Depends(get_db),
) -> list[CollectionListItem]:
    stmt = select(Collection).where(Collection.is_published.is_(True))
    if featured is not None:
        stmt = stmt.where(Collection.is_featured.is_(featured))
    stmt = stmt.order_by(Collection.position, Collection.created_at.desc())
    rows = db.execute(stmt.options(selectinload(Collection.items))).scalars().all()
    return [
        CollectionListItem(
            id=c.id,
            title=c.title,
            slug=c.slug,
            description=c.description,
            cover_url=file_public_url(c.cover),
            is_featured=c.is_featured,
            items_count=len(c.items),
        )
        for c in rows
    ]


@router.get(
    "/collections/{slug}",
    response_model=CollectionDetail,
    summary="Подборка с нотами",
)
def get_collection(slug: str, db: Session = Depends(get_db)) -> CollectionDetail:
    c = db.execute(
        select(Collection)
        .where(Collection.slug == slug, Collection.is_published.is_(True))
        .options(
            selectinload(Collection.items),
        )
    ).scalar_one_or_none()
    if not c:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Берём опубликованные ноты подборки в порядке position.
    scores = [
        item.score
        for item in c.items
        if item.score is not None and item.score.is_published
    ]
    return CollectionDetail(
        id=c.id,
        title=c.title,
        slug=c.slug,
        description=c.description,
        cover_url=file_public_url(c.cover),
        is_featured=c.is_featured,
        items_count=len(scores),
        scores=[_score_item(s) for s in scores],
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
    q: str | None = Query(None, description="Поиск по названию"),
    genre: str | None = Query(None, description="slug жанра"),
    style: str | None = Query(None, description="slug стиля"),
    instrument: str | None = Query(None, description="slug инструмента"),
    author: str | None = Query(None, description="slug автора"),
    sort: str = Query("new", pattern="^(new|popular|rating)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ScoreListResponse:
    stmt = select(Score).where(Score.is_published.is_(True))

    if q:
        stmt = stmt.where(Score.title.ilike(f"%{q}%"))
    if style:
        stmt = stmt.join(Score.style).where(Style.slug == style)
    if author:
        stmt = stmt.join(Score.author).where(Author.slug == author)
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

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()

    stmt = stmt.order_by(*_SORTS[sort]).offset((page - 1) * page_size).limit(page_size)
    rows = db.execute(stmt.options(selectinload(Score.author), selectinload(Score.style))).scalars().all()

    return ScoreListResponse(
        items=[_score_item(s) for s in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/scores/{slug}", response_model=ScoreDetail, summary="Карточка ноты")
def get_score(slug: str, db: Session = Depends(get_db)) -> ScoreDetail:
    s = db.execute(
        select(Score)
        .where(Score.slug == slug, Score.is_published.is_(True))
        .options(
            selectinload(Score.author),
            selectinload(Score.style),
            selectinload(Score.genres),
            selectinload(Score.instruments),
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
