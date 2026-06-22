"""Pydantic-схемы публичного каталожного API (camelCase, как остальной API)."""

from datetime import datetime

from pydantic import Field

from api.models import ApiModel


class AuthorOut(ApiModel):
    id: int
    name: str
    slug: str
    bio: str | None = None
    photo_url: str | None = None
    born: str | None = None
    died: str | None = None


class TermOut(ApiModel):
    """Справочный термин (жанр / стиль / инструмент)."""

    id: int
    name: str
    slug: str


class ScoreListItem(ApiModel):
    """Краткая карточка ноты для списков и подборок."""

    id: int
    title: str
    slug: str
    author: str | None = None
    cover_url: str | None = None
    format: str | None = None
    difficulty: str | None = None
    style: str | None = None
    rating_avg: float = 0.0
    rating_count: int = 0
    plays_count: int = 0


class ScoreDetail(ScoreListItem):
    """Полная карточка ноты."""

    description: str | None = None
    author_obj: AuthorOut | None = Field(default=None, alias="authorObj")
    genres: list[TermOut] = []
    instruments: list[TermOut] = []
    opus: str | None = None
    year: int | None = None
    lyricist: str | None = None
    license: str | None = None
    imslp_url: str | None = None
    music_xml_url: str | None = None
    midi_url: str | None = None
    audio_url: str | None = None
    pdf_url: str | None = None
    created_at: datetime | None = None


class ScoreListResponse(ApiModel):
    items: list[ScoreListItem]
    total: int
    page: int
    page_size: int


class CollectionListItem(ApiModel):
    id: int
    title: str
    slug: str
    description: str | None = None
    cover_url: str | None = None
    is_featured: bool = False
    items_count: int = 0


class CollectionDetail(CollectionListItem):
    scores: list[ScoreListItem] = []


class RateRequest(ApiModel):
    value: int = Field(ge=1, le=5, description="Оценка 1..5")


class ScoreStats(ApiModel):
    """Актуальные агрегаты ноты (ответ на rate/play)."""

    score_id: int
    rating_avg: float
    rating_count: int
    plays_count: int
    my_rating: int | None = None
