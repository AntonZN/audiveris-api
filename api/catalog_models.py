"""ORM-модели каталога нот.

Сущности:
  Author / Genre / Style / Instrument — справочники (админ ведёт сам)
  Score                               — нота (несколько файлов: обложка + mxl/midi/mp3/pdf)
  Collection / CollectionItem         — подборки (упорядоченный список нот)
  AppUser / Rating / PlayEvent        — пользователи приложения и их активность

Поля source/source_id/source_url/license/opus/... заложены под импорт внешних
корпусов (OpenScore Lieder, Mutopia) — см. scripts/import_lieder.py.

slug генерируется автоматически из name/title (см. событийные хуки внизу файла).
"""

import enum
from datetime import datetime

from fastapi_storages.integrations.sqlalchemy import FileType, ImageType
from slugify import slugify
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.db import Base
from api.storage import storage


# --------------------------------------------------------------------------- #
# Перечисления (native_enum=False -> хранятся как VARCHAR + CHECK, дружелюбно к
# create_all и дают выпадающий список в админке).
# --------------------------------------------------------------------------- #
class ScoreFormat(str, enum.Enum):
    solo = "solo"
    duet = "duet"
    ensemble = "ensemble"
    orchestra = "orchestra"
    choir = "choir"
    band = "band"
    other = "other"


class Difficulty(str, enum.Enum):
    beginner = "beginner"
    intermediate = "intermediate"
    advanced = "advanced"


class SourceType(str, enum.Enum):
    manual = "manual"
    openscore_lieder = "openscore_lieder"
    mutopia = "mutopia"
    omr = "omr"


# --------------------------------------------------------------------------- #
# Связи многие-ко-многим
# --------------------------------------------------------------------------- #
score_genres = Table(
    "score_genres",
    Base.metadata,
    Column("score_id", ForeignKey("scores.id", ondelete="CASCADE"), primary_key=True),
    Column("genre_id", ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True),
)

score_instruments = Table(
    "score_instruments",
    Base.metadata,
    Column("score_id", ForeignKey("scores.id", ondelete="CASCADE"), primary_key=True),
    Column("instrument_id", ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True),
)


# --------------------------------------------------------------------------- #
# Справочники
# --------------------------------------------------------------------------- #
class Author(Base):
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(280), unique=True, index=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo = Column(ImageType(storage=storage), nullable=True)
    born: Mapped[str | None] = mapped_column(String(32), nullable=True)
    died: Mapped[str | None] = mapped_column(String(32), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(32), nullable=True)
    wikidata: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wikipedia: Mapped[str | None] = mapped_column(String(512), nullable=True)
    imslp_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    source: Mapped[SourceType] = mapped_column(
        Enum(SourceType, native_enum=False, length=32), default=SourceType.manual
    )
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    scores: Mapped[list["Score"]] = relationship(back_populates="author")

    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_author_source"),)

    def __str__(self) -> str:  # отображение в админке
        return self.name


class Genre(Base):
    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True)

    def __str__(self) -> str:
        return self.name


class Style(Base):
    __tablename__ = "styles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True)

    scores: Mapped[list["Score"]] = relationship(back_populates="style")

    def __str__(self) -> str:
        return self.name


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True)

    def __str__(self) -> str:
        return self.name


# --------------------------------------------------------------------------- #
# Ноты
# --------------------------------------------------------------------------- #
class Score(Base):
    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str] = mapped_column(String(560), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("authors.id", ondelete="SET NULL"), nullable=True
    )
    style_id: Mapped[int | None] = mapped_column(
        ForeignKey("styles.id", ondelete="SET NULL"), nullable=True
    )

    format: Mapped[ScoreFormat | None] = mapped_column(
        Enum(ScoreFormat, native_enum=False, length=20), nullable=True
    )
    difficulty: Mapped[Difficulty | None] = mapped_column(
        Enum(Difficulty, native_enum=False, length=20), nullable=True
    )

    # Метаданные произведения
    opus: Mapped[str | None] = mapped_column(String(120), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lyricist: Mapped[str | None] = mapped_column(String(255), nullable=True)
    license: Mapped[str | None] = mapped_column(String(255), nullable=True)
    imslp_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Файлы. music_file (MusicXML/.mxl) — исходник; midi/audio — для прослушивания.
    cover = Column(ImageType(storage=storage), nullable=True)
    music_file = Column(FileType(storage=storage), nullable=True)
    midi_file = Column(FileType(storage=storage), nullable=True)
    audio_file = Column(FileType(storage=storage), nullable=True)
    pdf_file = Column(FileType(storage=storage), nullable=True)

    # Агрегаты активности (пересчитываются при оценке/проигрывании)
    rating_avg: Mapped[float] = mapped_column(Float, default=0.0)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    plays_count: Mapped[int] = mapped_column(Integer, default=0)

    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Происхождение (импорт)
    source: Mapped[SourceType] = mapped_column(
        Enum(SourceType, native_enum=False, length=32), default=SourceType.manual
    )
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    author: Mapped["Author | None"] = relationship(back_populates="scores")
    style: Mapped["Style | None"] = relationship(back_populates="scores")
    genres: Mapped[list[Genre]] = relationship(secondary=score_genres)
    instruments: Mapped[list[Instrument]] = relationship(secondary=score_instruments)
    # Подборки, в которые входит нота (обратная сторона Collection.scores) —
    # чтобы выбирать подборки прямо в форме ноты.
    collections: Mapped[list["Collection"]] = relationship(
        secondary="collection_items",
        back_populates="scores",
        overlaps="items,collection,score",
    )

    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_score_source"),)

    def __str__(self) -> str:
        return self.title


# --------------------------------------------------------------------------- #
# Подборки
# --------------------------------------------------------------------------- #
class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    slug: Mapped[str] = mapped_column(String(560), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover = Column(ImageType(storage=storage), nullable=True)

    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    source: Mapped[SourceType] = mapped_column(
        Enum(SourceType, native_enum=False, length=32), default=SourceType.manual
    )
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # items — упорядоченное чтение для API (read-only: запись идёт через .scores,
    # либо через отдельную вьюху CollectionItem для тонкой настройки позиций).
    items: Mapped[list["CollectionItem"]] = relationship(
        back_populates="collection",
        order_by="CollectionItem.position",
        viewonly=True,
        overlaps="scores,collections",
    )
    # scores — редактируемая M2M-связь для админки (инлайн-добавление нот в подборку).
    # Пишет строки в collection_items (position=0 по умолчанию; точный порядок — во
    # вьюхе «Состав подборок»). Парная сторона — Score.collections.
    scores: Mapped[list["Score"]] = relationship(
        secondary="collection_items",
        order_by="CollectionItem.position",
        back_populates="collections",
        overlaps="items,collection,score",
    )

    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_collection_source"),)

    def __str__(self) -> str:
        return self.title


class CollectionItem(Base):
    """Нота в подборке с порядковым номером (управляется отдельной вьюхой в админке)."""

    __tablename__ = "collection_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), index=True
    )
    score_id: Mapped[int] = mapped_column(ForeignKey("scores.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)

    collection: Mapped[Collection] = relationship(back_populates="items", overlaps="scores,collections")
    score: Mapped[Score] = relationship(overlaps="scores,collections")

    __table_args__ = (
        UniqueConstraint("collection_id", "score_id", name="uq_collection_item"),
    )

    def __str__(self) -> str:
        return f"#{self.position}"


# --------------------------------------------------------------------------- #
# Пользователи приложения и активность
# --------------------------------------------------------------------------- #
class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __str__(self) -> str:
        return self.device_id


class Rating(Base):
    __tablename__ = "ratings"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), index=True)
    score_id: Mapped[int] = mapped_column(ForeignKey("scores.id", ondelete="CASCADE"), index=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..5
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("user_id", "score_id", name="uq_rating_user_score"),)


class PlayEvent(Base):
    __tablename__ = "play_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    score_id: Mapped[int] = mapped_column(ForeignKey("scores.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# --------------------------------------------------------------------------- #
# Авто-slug из name/title
# --------------------------------------------------------------------------- #
# (модель, имя поля-источника) -> slug строится из этого поля и делается уникальным.
_SLUGGED = {
    Author: "name",
    Genre: "name",
    Style: "name",
    Instrument: "name",
    Score: "title",
    Collection: "title",
}


def _unique_slug(connection, model, base: str, current_id) -> str:
    table = model.__table__
    base = base or "item"
    # Slug'и, уже выданные в этом соединении, но возможно ещё не во flush'е БД
    # (важно для батчевых insertmany, где соседи партии не видны через SELECT).
    seen = connection.info.setdefault("_slug_seen", {}).setdefault(table.name, set())
    candidate = base
    suffix = 2
    while True:
        if candidate not in seen:
            stmt = select(table.c.id).where(table.c.slug == candidate)
            if current_id is not None:
                stmt = stmt.where(table.c.id != current_id)
            if connection.execute(stmt).first() is None:
                seen.add(candidate)
                return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


def _assign_slug(connection, target, source_field) -> None:
    desired_base = slugify(getattr(target, source_field, None) or "") or "item"
    target.slug = _unique_slug(connection, type(target), desired_base, getattr(target, "id", None))


def _register_slug_events() -> None:
    for model, field in _SLUGGED.items():
        @event.listens_for(model, "before_insert")
        def _before_insert(mapper, connection, target, _field=field):
            _assign_slug(connection, target, _field)

        @event.listens_for(model, "before_update")
        def _before_update(mapper, connection, target, _field=field):
            if not getattr(target, "slug", None):
                _assign_slug(connection, target, _field)


_register_slug_events()
