#!/usr/bin/env python3
"""Импорт корпуса OpenScore Lieder (https://github.com/OpenScore/Lieder) в каталог.

Корпус — 1300+ песен, лицензия CC0. Метаданные лежат в TSV
(composers.tsv / sets.tsv / scores.tsv). В репозитории рядом с .mscx уже
ЗАКОММИЧЕНЫ готовые .mxl (MusicXML) — путь scores/<path>/lc<id>.mxl, — поэтому
MuseScore для получения нот НЕ нужен: .mxl качаются прямо по HTTP.

Скрипт идемпотентен: повторный запуск обновляет уже импортированные записи
(ключ — source='openscore_lieder' + source_id). Уже привязанные файлы не качаются заново.

Режимы
------
1) По умолчанию: тянет TSV по HTTP, заводит авторов/подборки/ноты И скачивает
   .mxl с GitHub, привязывая как music_file.

       python scripts/import_lieder.py --limit 50

   Только метаданные, без .mxl (быстро):
       python scripts/import_lieder.py --limit 50 --no-mxl

2) Из локального клона (--repo-dir): берёт TSV и медиа из репозитория на диске.
   Привязывает найденные рядом .mxl и (если вы сами сконвертировали MuseScore'ом)
   .mid/.mp3/.pdf.

       git clone https://github.com/OpenScore/Lieder
       python scripts/import_lieder.py --repo-dir /path/to/Lieder

Фото авторов
------------
По умолчанию тянутся портреты композиторов: из колонки image в composers.tsv
(Wikimedia Commons), иначе из Wikidata (P18). Ужимаются до 600px JPEG в Author.photo.
Отключить: --no-photos.

Подборки (sets)
---------------
Каждый «set» Lieder — это группа песен ОДНОГО композитора (опус/цикл, либо свалка
«Other songs by …»). ПО УМОЛЧАНИЮ подборки НЕ создаются — их ведут вручную в админке.
Опционально: --sets cycles (только настоящие циклы, >=2 песни) или --sets all (все 249).
Песни импортируются в любом случае, независимо от режима sets.

Запуск в docker (каталожная БД внутри compose):
       docker compose exec audiveris-api python scripts/import_lieder.py --limit 50
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path
from urllib.parse import quote

# Чтобы `import api...` работал и при запуске как ./scripts/import_lieder.py.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from api.catalog_models import (  # noqa: E402
    Author,
    Collection,
    CollectionItem,
    Genre,
    Instrument,
    Score,
    SourceType,
)
from api.audio_preview import render_audio_preview  # noqa: E402
from api.db import SessionLocal, init_db  # noqa: E402
from api.preview import render_cover_png  # noqa: E402
from api.storage import storage  # noqa: E402

RAW_BASE = "https://raw.githubusercontent.com/OpenScore/Lieder/main"
SOURCE = SourceType.openscore_lieder
LICENSE = "CC0 1.0"
MEDIA_EXT = {"music_file": ".mxl", "midi_file": ".mid", "audio_file": ".mp3", "pdf_file": ".pdf"}


# --------------------------------------------------------------------------- #
# Загрузка TSV
# --------------------------------------------------------------------------- #
def _read_tsv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def load_tables(repo_dir: Path | None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for name in ("composers", "sets", "scores"):
        if repo_dir:
            text = (repo_dir / "data" / f"{name}.tsv").read_text(encoding="utf-8")
        else:
            resp = httpx.get(f"{RAW_BASE}/data/{name}.tsv", timeout=60.0)
            resp.raise_for_status()
            text = resp.text
        out[name] = _read_tsv(text)
        print(f"  {name}.tsv: {len(out[name])} rows")
    return out


# --------------------------------------------------------------------------- #
# Идемпотентный upsert по (source, source_id)
# --------------------------------------------------------------------------- #
def _get_by_source(db: Session, model, source_id: str):
    return db.execute(
        select(model).where(model.source == SOURCE, model.source_id == str(source_id))
    ).scalar_one_or_none()


def _ensure_term(db: Session, model, name: str):
    obj = db.execute(select(model).where(model.name == name)).scalar_one_or_none()
    if obj is None:
        obj = model(name=name)
        db.add(obj)
        db.flush()
    return obj


def _want_set(row: dict, sets_mode: str) -> bool:
    """Создавать ли подборку из этого set.

    sets_mode: 'cycles' — только настоящие циклы/опусы (>=2 песни, не «Other songs by»);
    'all' — любой set; 'none' — никакие.
    """
    if sets_mode == "none":
        return False
    if sets_mode == "all":
        return True
    # cycles: отсекаем свалки «Other songs by …» и одиночные сеты.
    if (row.get("name") or "").startswith("Other songs by"):
        return False
    try:
        return int(row.get("scores") or 0) >= 2
    except ValueError:
        return False


class _LocalUpload:
    """Минимальный upload-объект для FileType (нужны .file и .filename).

    Держит открытый файловый дескриптор, поэтому после записи в хранилище
    (db.flush) его обязательно закрыть через close(), иначе на полном импорте
    корпуса (1300+ нот × до 4 файлов) исчерпываются файловые дескрипторы.
    """

    def __init__(self, path: Path, filename: str) -> None:
        self.file = open(path, "rb")
        self.filename = filename

    def close(self) -> None:
        self.file.close()


class _BytesUpload:
    """Upload-объект из байтов в памяти (для скачанных по HTTP файлов)."""

    def __init__(self, content: bytes, filename: str) -> None:
        self.file = io.BytesIO(content)
        self.filename = filename


def _download_mxl(client: httpx.Client, score: Score, path: str) -> bool:
    """Скачать готовый .mxl из репозитория и привязать как music_file.

    Файл лежит по пути scores/<path>/lc<source_id>.mxl. Уже привязанный — пропускаем.
    """
    if getattr(score, "music_file") or not path:
        return False
    url = f"{RAW_BASE}/scores/{quote(path, safe='/')}/lc{score.source_id}.mxl"
    try:
        resp = client.get(url)
    except httpx.HTTPError:
        return False
    if resp.status_code != 200 or not resp.content:
        return False
    score.music_file = _BytesUpload(resp.content, f"lieder-{score.source_id}.mxl")
    return True


def _resize_jpeg(content: bytes, box: tuple[int, int] = (600, 600)) -> bytes | None:
    """Перекодировать картинку в компактный JPEG (с ресайзом по большей стороне)."""
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(content)).convert("RGB")
        im.thumbnail(box)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return None


def _wikidata_image_url(client: httpx.Client, qid: str) -> str | None:
    """URL портрета композитора из Wikidata (свойство P18 -> Wikimedia Commons)."""
    if not qid:
        return None
    try:
        resp = client.get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
        if resp.status_code != 200:
            return None
        claims = resp.json()["entities"][qid]["claims"]
        p18 = claims.get("P18")
        if not p18:
            return None
        filename = p18[0]["mainsnak"]["datavalue"]["value"]
    except Exception:
        return None
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(filename.replace(' ', '_'))}?width=600"


def _fetch_photo(client: httpx.Client, author: Author, image_url: str, wikidata: str) -> bool:
    """Скачать фото автора (из колонки image, иначе из Wikidata) и привязать как photo."""
    if author.photo:
        return False
    url = image_url or _wikidata_image_url(client, wikidata)
    if not url:
        return False
    try:
        resp = client.get(url)
    except httpx.HTTPError:
        return False
    if resp.status_code != 200 or not resp.content:
        return False
    jpeg = _resize_jpeg(resp.content)
    if not jpeg:
        return False
    author.photo = _BytesUpload(jpeg, f"author-{author.source_id}.jpg")
    return True


def _generate_cover(db: Session, score: Score) -> bool:
    """Сгенерировать обложку из привязанного .mxl (verovio -> PNG)."""
    if score.cover:
        return False
    # После flush атрибут ещё хранит upload-объект; перечитываем из БД, чтобы
    # получить StorageFile с реальным путём к записанному .mxl.
    db.refresh(score, attribute_names=["music_file"])
    if not score.music_file:
        return False
    music_path = Path(str(score.music_file))  # абсолютный путь в хранилище
    if not music_path.exists():
        return False
    png = render_cover_png(music_path)
    if not png:
        return False
    score.cover = _BytesUpload(png, f"lieder-{score.source_id}.png")
    return True


def _generate_audio_preview(db: Session, score: Score) -> bool:
    """Сгенерировать короткий MP3 из привязанного .mxl, если его ещё нет."""
    if score.audio_file:
        return False
    db.refresh(score, attribute_names=["music_file"])
    if not score.music_file:
        return False
    music_path = Path(str(score.music_file))
    if not music_path.is_file():
        return False
    mp3 = render_audio_preview(music_path)
    if not mp3:
        return False
    score.audio_file = _BytesUpload(mp3, f"lieder-{score.source_id}-preview.mp3")
    return True


def _attach_media(score: Score, score_dir: Path) -> list[_LocalUpload]:
    """Привязать сконвертированные файлы из локального каталога ноты.

    FileType сам запишет файл в хранилище каталога. Уже привязанные поля
    пропускаем, чтобы повторный импорт не плодил копии.

    Возвращает созданные upload-объекты — их нужно закрыть после db.flush().
    """
    if not score_dir.is_dir():
        return []
    uploads: list[_LocalUpload] = []
    for field, ext in MEDIA_EXT.items():
        if getattr(score, field):
            continue
        matches = sorted(score_dir.glob(f"*{ext}"))
        if not matches:
            continue
        upload = _LocalUpload(matches[0], f"lieder-{score.source_id}{ext}")
        setattr(score, field, upload)
        uploads.append(upload)
    return uploads


def run(
    repo_dir: Path | None,
    limit: int | None,
    download_mxl: bool,
    generate_cover: bool,
    generate_audio: bool = False,
    sets_mode: str = "none",
    download_photos: bool = True,
) -> None:
    init_db()
    tables = load_tables(repo_dir)
    # User-Agent обязателен для Wikimedia/Wikidata (без него отдают 403).
    client = httpx.Client(
        timeout=60.0,
        follow_redirects=True,
        headers={"User-Agent": "audiveris-catalog-importer/1.0 (dev@dev.com)"},
    )
    db = SessionLocal()
    try:
        lied = _ensure_term(db, Genre, "Lied")
        voice = _ensure_term(db, Instrument, "Voice")
        piano = _ensure_term(db, Instrument, "Piano")

        # 1) Композиторы -> авторы (+ фото с Wikimedia/Wikidata)
        author_by_src: dict[str, Author] = {}
        photos_made = 0
        for row in tables["composers"]:
            sid = row["id"]
            author = _get_by_source(db, Author, sid) or Author(source=SOURCE, source_id=sid)
            author.name = row.get("name") or row.get("path", "Unknown")
            author.born = row.get("born") or None
            author.died = row.get("died") or None
            author.gender = row.get("gender") or None
            author.bio = row.get("desc") or None
            author.wikidata = row.get("wikidata") or None
            author.wikipedia = row.get("wikipedia") or None
            author.imslp_url = row.get("imslp") or None
            author.source_url = row.get("link") or None
            if download_photos and _fetch_photo(
                client, author, row.get("image") or "", row.get("wikidata") or ""
            ):
                photos_made += 1
            db.add(author)
            author_by_src[sid] = author
        db.flush()
        print(f"  авторов с фото: {photos_made}/{len(author_by_src)}")

        # 2) Sets -> подборки (+ запоминаем композитора набора).
        # set_composer строим для ВСЕХ сетов (нужно для привязки автора к ноте),
        # а подборку создаём только для нужных (по умолчанию — настоящие циклы).
        collection_by_src: dict[str, Collection] = {}
        set_composer: dict[str, str] = {}
        skipped_sets = 0
        for row in tables["sets"]:
            sid = row["id"]
            set_composer[sid] = row.get("composer_id", "")
            if not _want_set(row, sets_mode):
                skipped_sets += 1
                continue
            col = _get_by_source(db, Collection, sid) or Collection(source=SOURCE, source_id=sid)
            col.title = row.get("name") or row.get("path", "Untitled set")
            col.source_url = row.get("link") or None
            db.add(col)
            collection_by_src[sid] = col
        db.flush()

        # 3) Scores -> ноты
        rows = tables["scores"]
        if limit:
            rows = rows[:limit]

        positions: dict[int, int] = {}
        imported = 0
        media_attached = 0
        covers_made = 0
        audio_previews_made = 0
        for row in rows:
            sid = row["id"]
            score = _get_by_source(db, Score, sid) or Score(source=SOURCE, source_id=sid)
            score.title = row.get("name") or "Untitled"
            score.license = LICENSE
            score.source_url = row.get("link") or None
            score.imslp_url = row.get("imslp") or None

            set_id = row.get("set_id", "")
            composer_id = set_composer.get(set_id, "")
            author = author_by_src.get(composer_id)
            if author is not None:
                score.author = author

            if lied not in score.genres:
                score.genres.append(lied)
            for ins in (voice, piano):
                if ins not in score.instruments:
                    score.instruments.append(ins)

            # Медиа: из локального клона или скачиваем готовый .mxl с GitHub.
            path = row.get("path") or ""
            local_uploads: list[_LocalUpload] = []
            if repo_dir and path:
                local_uploads = _attach_media(score, repo_dir / "scores" / path)
                media_attached += len(local_uploads)
            elif download_mxl and _download_mxl(client, score, path):
                media_attached += 1

            db.add(score)
            db.flush()  # пишет music_file на диск -> доступен путь для рендера обложки
            # Файлы записаны в хранилище — закрываем открытые дескрипторы _LocalUpload.
            for upload in local_uploads:
                upload.close()

            # Обложка: рендерим из привязанного .mxl через verovio.
            if generate_cover and _generate_cover(db, score):
                covers_made += 1

            # Аудио опционально: массовый импорт не должен неожиданно становиться
            # тяжёлым. Готовый audio_file из локального клона не перезаписывается.
            if generate_audio and _generate_audio_preview(db, score):
                audio_previews_made += 1

            # Привязка к подборке (sets) с сохранением порядка
            col = collection_by_src.get(set_id)
            if col is not None:
                exists = db.execute(
                    select(CollectionItem).where(
                        CollectionItem.collection_id == col.id,
                        CollectionItem.score_id == score.id,
                    )
                ).scalar_one_or_none()
                if exists is None:
                    pos = positions.get(col.id, 0)
                    db.add(CollectionItem(collection_id=col.id, score_id=score.id, position=pos))
                    positions[col.id] = pos + 1

            imported += 1
            if imported % 100 == 0:
                db.commit()
                print(f"  ...{imported} scores")

        db.commit()
        print(
            f"Готово: авторов={len(author_by_src)} (фото={photos_made}) "
            f"подборок={len(collection_by_src)} (пропущено sets={skipped_sets}) "
            f"нот={imported} .mxl/медиа привязано={media_attached} обложек={covers_made} "
            f"аудиопревью={audio_previews_made}"
        )
    finally:
        client.close()
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Импорт OpenScore Lieder в каталог")
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=None,
        help="Путь к локальному клону Lieder (медиа берётся с диска вместо HTTP).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Импортировать не более N нот")
    parser.add_argument(
        "--no-mxl",
        action="store_true",
        help="Не скачивать .mxl по HTTP (только метаданные). Игнорируется при --repo-dir.",
    )
    parser.add_argument(
        "--no-cover",
        action="store_true",
        help="Не генерировать обложки из .mxl через verovio.",
    )
    parser.add_argument(
        "--audio-previews",
        action="store_true",
        help="Сгенерировать отсутствующие короткие MP3-превью из .mxl.",
    )
    parser.add_argument(
        "--no-photos",
        action="store_true",
        help="Не скачивать фото авторов с Wikimedia/Wikidata.",
    )
    parser.add_argument(
        "--sets",
        choices=["none", "cycles", "all"],
        default="none",
        help=(
            "Создавать ли подборки из Lieder sets: none — не создавать (по умолчанию, "
            "подборки ведутся вручную в админке); cycles — только настоящие циклы; all — все."
        ),
    )
    args = parser.parse_args()
    run(
        args.repo_dir,
        args.limit,
        download_mxl=not args.no_mxl,
        generate_cover=not args.no_cover,
        generate_audio=args.audio_previews,
        sets_mode=args.sets,
        download_photos=not args.no_photos,
    )


if __name__ == "__main__":
    main()
