"""Слой работы с БД каталога (SQLAlchemy 2.x, синхронный движок).

Синхронный engine выбран осознанно: SQLAdmin отлично работает с sync-сессиями,
а публичные catalog-эндпоинты объявлены как обычные `def` — FastAPI выполняет их
в threadpool, поэтому блокирующие вызовы БД не стопорят event loop.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from api.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI-зависимость: сессия БД на время запроса."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations() -> None:
    """Привести схему БД к последней ревизии Alembic (`upgrade head`).

    Идемпотентно: если схема уже на head, ничего не делает. Используется и
    сервисом `migrate` (в compose), и стартом приложения как страховка для
    локального запуска без docker-оркестрации.
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    ini_path = Path(__file__).resolve().parent / "alembic.ini"
    cfg = Config(str(ini_path))
    # script_location в ini относителен WORKDIR; задаём абсолютный, чтобы
    # миграции находились независимо от текущей директории запуска.
    cfg.set_main_option("script_location", str(ini_path.parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")


# Совместимость со стартовым кодом: схему теперь ведёт Alembic.
def init_db() -> None:
    run_migrations()
