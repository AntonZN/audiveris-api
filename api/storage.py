"""Хранилище загружаемых через админку файлов каталога.

Файлы кладём в ``settings.catalog_media_dir`` (внутри media_root), поэтому их
раздаёт тот же статик-роут ``/media/*`` (Caddy -> /storage/out), что и выходы OMR.
URL для мобильного клиента строим из media_base_url / media_path_prefix.
"""

from pathlib import Path
from urllib.parse import quote

from fastapi_storages import FileSystemStorage

from api.config import settings


class CatalogStorage(FileSystemStorage):
    # Не перезаписывать существующие файлы: при совпадении имён fastapi-storages
    # добавит суффикс. Так загрузки из админки не затрут друг друга.
    OVERWRITE_EXISTING_FILES = False


# CatalogStorage (FileSystemStorage) сам создаёт директорию в __init__.
storage = CatalogStorage(path=settings.catalog_media_dir)


def file_public_url(value: object | None) -> str | None:
    """Построить публичный URL для значения FileType/ImageType-колонки.

    StorageFile/StorageImage наследуют str и в str() отдают абсолютный путь;
    обычная строка — это имя файла. Обрабатываем оба случая.
    """
    if not value:
        return None
    raw = str(value)
    if not raw:
        return None

    full = Path(raw)
    if not full.is_absolute():
        full = Path(settings.catalog_media_dir) / raw
    try:
        rel = full.relative_to(Path(settings.media_root))
    except ValueError:
        # catalog_media_dir вне media_root — отдаём хотя бы относительный путь.
        rel = Path("catalog") / full.name

    rel_posix = quote(rel.as_posix())
    base = settings.media_base_url.rstrip("/")
    prefix = settings.media_path_prefix.strip("/")
    if prefix:
        return f"{base}/{prefix}/{rel_posix}"
    return f"{base}/{rel_posix}"
