#!/usr/bin/env python3
"""Безопасный импорт PDMX в каталог нот.

По умолчанию скрипт выполняет только dry-run: читает PDMX.csv, применяет
лицензионные и dedup-фильтры и показывает, как авторы/теги/инструменты будут
сопоставлены с существующим каталогом. Для записи в БД нужен явный ``--apply``.

Рекомендуемый первый запуск::

    python scripts/import_pdmx.py \
        --catalog-export pdmx_prod_catalog.json \
        --subset rated-deduplicated

Пилотный импорт метаданных (новые записи остаются неопубликованными)::

    docker compose run --rm \
        -v "$PWD/PDMX.csv:/srv/PDMX.csv:ro" \
        audiveris-api python scripts/import_pdmx.py \
        --csv /srv/PDMX.csv --subset rated-deduplicated --limit 50 --apply

MXL берутся только из уже скачанного и распакованного каталога PDMX. Скрипт
ничего не скачивает с Zenodo; ``--attach-mxl`` требует ``--pdmx-dir``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PDMX_RECORD_URL = "https://zenodo.org/records/15571083"
PDMX_DOI_URL = "https://doi.org/10.5281/zenodo.15571083"
DEFAULT_CSV = _ROOT / "PDMX.csv"
DEFAULT_EXPORT = _ROOT / "pdmx_prod_catalog.json"
DEFAULT_ALIASES = Path(__file__).resolve().parent / "data" / "pdmx_author_aliases.tsv"
DEFAULT_AUTHOR_PROFILES = Path(__file__).resolve().parent / "data" / "pdmx_authors.tsv"

MISSING_VALUES = frozenset({"", "na", "n/a", "nan", "none", "null"})
SUBSET_COLUMNS = {
    "rated-deduplicated": "subset:rated_deduplicated",
    "deduplicated": "subset:deduplicated",
    "all-valid": "subset:all_valid",
}
LICENSE_NAMES = {
    "publicdomain": "Public Domain Mark 1.0",
    "cc-zero": "CC0 1.0",
}

# Из PDMX tags берём только понятные каталожные признаки. Имена композиторов,
# названия библиотек и платформенные метки намеренно не импортируются.
TAG_WHITELIST = frozenset(
    {
        "baroque",
        "christmas",
        "classical",
        "dance",
        "duet",
        "easy",
        "etude",
        "folk",
        "hymn",
        "jazz",
        "orchestra",
        "quartet",
        "romantic",
        "sacred",
        "satb",
        "solo",
    }
)
TAG_DISPLAY_NAMES = {
    "baroque": "Baroque",
    "christmas": "Christmas",
    "classical": "classical",
    "dance": "Dance",
    "duet": "Duet",
    "easy": "Easy",
    "etude": "Etude",
    "folk": "Folk",
    "hymn": "Hymn",
    "jazz": "Jazz",
    "orchestra": "Orchestra",
    "quartet": "Quartet",
    "romantic": "Romantic",
    "sacred": "Sacred",
    "satb": "SATB",
    "solo": "Solo",
}

INSTRUMENT_DISPLAY_NAMES = {
    "alto-saxophone": "Alto Saxophone",
    "baritone-saxophone": "Baritone Saxophone",
    "bass": "Bass",
    "bassoon": "Bassoon",
    "brass": "Brass",
    "cello": "Cello",
    "clarinet": "Clarinet",
    "double-bass": "Double Bass",
    "english-horn": "English Horn",
    "flute": "Flute",
    "french-horn": "French Horn",
    "guitar": "Guitar",
    "oboe": "Oboe",
    "organ": "Organ",
    "piano": "Piano",
    "piccolo": "Piccolo",
    "recorder": "Recorder",
    "soprano-saxophone": "Soprano Saxophone",
    "strings": "Strings",
    "tenor-saxophone": "Tenor Saxophone",
    "trombone": "Trombone",
    "trumpet": "Trumpet",
    "tuba": "Tuba",
    "viola": "Viola",
    "violin": "Violin",
    "voice": "Voice",
}

INSTRUMENT_TAGS = {
    "bass": ("bass", "double-bass", "contrabass"),
    "cello": ("cello",),
    "clarinet": ("clarinet",),
    "flute": ("flute",),
    "guitar": ("guitar",),
    "piano": ("piano",),
    "saxophone": ("saxophone", "sax"),
    "trombone": ("trombone",),
    "trumpet": ("trumpet",),
    "viola": ("viola",),
    "violin": ("violin",),
}

_NON_PERSON_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\??$",
        r"\banon(?:ymous|im)?\b",
        r"\btrad(?:itional|itionell|icional)?\b",
        r"\bunattributed\b",
        r"\bunknown\b",
        r"\burheber unbekannt\b",
        r"^composer$",
        r"^misc(?:ellaneous)?\b",
        r"\btranscribed by\b",
        r"\barranged by\b",
        r"^arr(?:\.|gt\b)",
        r"\brealizations by\b",
        r"\bharmonisation\b",
        r"\bharmonisatie\b",
    )
)

SOURCE_METADATA_FIELDS = (
    "path",
    "metadata",
    "mxl",
    "pdf",
    "mid",
    "version",
    "song_name",
    "title",
    "subtitle",
    "artist_name",
    "composer_name",
    "genres",
    "groups",
    "tags",
    "complexity",
    "n_tracks",
    "tracks",
    "song_length.seconds",
    "song_length.bars",
    "song_length.beats",
    "n_notes",
    "notes_per_bar",
    "n_lyrics",
    "has_lyrics",
    "rating",
    "n_ratings",
    "n_views",
    "license",
    "license_url",
    "license_conflict",
    "is_best_unique_arrangement",
    "subset:no_license_conflict",
    "subset:all_valid",
    "subset:deduplicated",
    "subset:rated",
    "subset:rated_deduplicated",
)


def clean_value(value: str | None) -> str | None:
    value = " ".join((value or "").strip().split())
    return None if value.casefold() in MISSING_VALUES else value


def normalized_key(value: str | None) -> str:
    """Нормализовать имя/slug для консервативного точного сопоставления."""
    value = clean_value(value)
    if not value:
        return ""
    if value.count(",") == 1 and not re.search(r"\d", value):
        family, given = (part.strip() for part in value.split(",", 1))
        if family and given:
            value = f"{given} {family}"
    value = re.sub(r"\([^)]*\d{4}[^)]*\)", " ", value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", value))


def canonical_author_name(value: str | None) -> str | None:
    """Убрать из имени годы жизни, opus и безопасные служебные префиксы."""
    value = clean_value(value)
    if not value:
        return None
    value = re.sub(r"^(?:music|composed)\s+by\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^by\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\([^)]*\d{4}[^)]*\)", "", value)
    value = re.sub(r"\([^)]*\d{8}[^)]*\)", "", value)
    value = re.sub(r"\s*c\.?\s*\d{4}\s+c\.?\s*\d{4}.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*\d{4}\s*[-–]\s*\d{4}.*$", "", value)
    value = re.sub(r"\s*op(?:us)?\.?\s*\d.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[\s,.;:]+$", "", value)
    return clean_value(value)


def slug_key(value: str | None) -> str:
    return normalized_key(value).replace(" ", "-")


def is_non_person_author(value: str | None) -> bool:
    value = clean_value(value)
    return not value or any(pattern.search(value) for pattern in _NON_PERSON_PATTERNS)


def looks_like_person(value: str | None) -> bool:
    value = clean_value(value)
    if is_non_person_author(value) or not value or re.search(r"\d|https?://", value):
        return False
    words = re.findall(r"[^\W\d_]+", value, re.UNICODE)
    return 2 <= len(words) <= 7 and len(value) <= 255


def pdmx_source_id(row: dict[str, str]) -> str:
    """Исходный MuseScore ID из metadata path; fallback — CID data path."""
    metadata = clean_value(row.get("metadata"))
    if metadata:
        return PurePosixPath(metadata).stem
    path = clean_value(row.get("path"))
    if path:
        return PurePosixPath(path).stem
    raise ValueError("PDMX row has neither metadata nor path")


def author_source_id(name: str) -> str:
    digest = hashlib.sha256(normalized_key(name).encode("utf-8")).hexdigest()[:32]
    return f"composer:{digest}"


def bool_value(value: str | None) -> bool:
    return (value or "").strip().casefold() == "true"


def row_selected(row: dict[str, str], subset: str) -> bool:
    return (
        bool_value(row.get("subset:no_license_conflict"))
        and bool_value(row.get("subset:all_valid"))
        and bool_value(row.get(SUBSET_COLUMNS[subset]))
    )


def iter_selected_rows(
    csv_path: Path,
    subset: str,
    limit: int | None = None,
) -> Iterator[dict[str, str]]:
    selected = 0
    with csv_path.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            if not row_selected(row, subset):
                continue
            yield row
            selected += 1
            if limit is not None and selected >= limit:
                return


def split_metadata_values(value: str | None) -> list[str]:
    value = clean_value(value)
    if not value:
        return []
    return [part.strip() for part in value.split("-") if part.strip()]


def track_programs(value: str | None) -> list[int]:
    programs: list[int] = []
    for part in split_metadata_values(value):
        try:
            program = int(part)
        except ValueError:
            continue
        if 0 <= program <= 127:
            programs.append(program)
    return programs


def gm_instrument_candidates(program: int) -> tuple[str, ...]:
    """Вернуть кандидаты slug справочника для General MIDI program number."""
    specific = {
        40: ("violin",),
        41: ("viola",),
        42: ("cello",),
        43: ("double-bass", "contrabass", "bass"),
        52: ("choir", "voice", "vocals"),
        53: ("voice", "vocals", "choir"),
        56: ("trumpet",),
        57: ("trombone",),
        58: ("tuba",),
        60: ("french-horn", "horn"),
        64: ("soprano-saxophone", "saxophone", "sax"),
        65: ("alto-saxophone", "saxophone", "sax"),
        66: ("tenor-saxophone", "saxophone", "sax"),
        67: ("baritone-saxophone", "saxophone", "sax"),
        68: ("oboe",),
        69: ("english-horn", "horn"),
        70: ("bassoon",),
        71: ("clarinet",),
        72: ("piccolo", "flute"),
        73: ("flute",),
        74: ("recorder",),
    }
    if program in specific:
        return specific[program]
    families = (
        (range(0, 8), ("piano",)),
        (range(16, 24), ("organ",)),
        (range(24, 32), ("guitar",)),
        (range(32, 40), ("bass", "double-bass", "contrabass")),
        (range(44, 52), ("strings", "string-ensemble")),
        (range(54, 56), ("voice", "vocals", "choir")),
        (range(59, 64), ("brass",)),
        (range(75, 80), ("flute", "woodwind")),
    )
    for programs, candidates in families:
        if program in programs:
            return candidates
    return ()


def instrument_candidate_groups(row: dict[str, str]) -> list[tuple[str, ...]]:
    groups: list[tuple[str, ...]] = []
    for program in track_programs(row.get("tracks")):
        candidates = gm_instrument_candidates(program)
        if candidates and candidates not in groups:
            groups.append(candidates)
    for raw_tag in split_metadata_values(row.get("tags")):
        candidates = INSTRUMENT_TAGS.get(slug_key(raw_tag))
        if candidates and candidates not in groups:
            groups.append(candidates)
    return groups


def desired_instrument_slugs(row: dict[str, str]) -> set[str]:
    return {candidates[0] for candidates in instrument_candidate_groups(row)}


def map_difficulty(value: str | None) -> int | None:
    try:
        complexity = int(float(value or ""))
    except ValueError:
        return None
    if complexity <= 1:
        return 1
    if complexity == 2:
        return 2
    return 3


def map_license(row: dict[str, str]) -> tuple[str | None, str | None]:
    code = (clean_value(row.get("license")) or "").casefold()
    return LICENSE_NAMES.get(code, clean_value(row.get("license"))), clean_value(
        row.get("license_url")
    )


def source_metadata(row: dict[str, str]) -> dict:
    metadata = {
        key: clean_value(row.get(key))
        for key in SOURCE_METADATA_FIELDS
        if clean_value(row.get(key)) is not None
    }
    metadata["dataset"] = "PDMX"
    metadata["dataset_doi"] = PDMX_DOI_URL
    return metadata


@dataclass(frozen=True)
class CatalogAuthor:
    id: int
    name: str
    slug: str = ""
    wikidata: str | None = None
    source: str | None = None
    source_id: str | None = None


@dataclass
class CatalogSnapshot:
    authors: list[CatalogAuthor] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)
    genres: set[str] = field(default_factory=set)
    instruments: set[str] = field(default_factory=set)
    _author_index: dict[str, list[CatalogAuthor]] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    @classmethod
    def from_json(cls, path: Path) -> "CatalogSnapshot":
        payload = json.loads(path.read_text(encoding="utf-8"))
        authors = [
            CatalogAuthor(
                id=int(row["id"]),
                name=row["name"],
                slug=row.get("slug") or "",
                wikidata=row.get("wikidata"),
                source=row.get("source"),
                source_id=row.get("source_id"),
            )
            for row in payload.get("authors", [])
        ]

        def slugs(name: str) -> set[str]:
            return {
                slug_key(row.get("slug") or row.get("name"))
                for row in payload.get(name, [])
                if row.get("slug") or row.get("name")
            }

        return cls(
            authors=authors,
            tags=slugs("tags"),
            genres=slugs("genres"),
            instruments=slugs("instruments"),
        )

    def authors_by_key(self) -> dict[str, list[CatalogAuthor]]:
        if self._author_index is not None:
            return self._author_index
        result: dict[str, list[CatalogAuthor]] = defaultdict(list)
        for author in self.authors:
            result[normalized_key(author.name)].append(author)
        self._author_index = result
        return self._author_index

    def authors_by_wikidata(self) -> dict[str, list[CatalogAuthor]]:
        result: dict[str, list[CatalogAuthor]] = defaultdict(list)
        for author in self.authors:
            if author.wikidata:
                result[author.wikidata.casefold()].append(author)
        return result


def load_aliases(path: Path) -> dict[str, str | None]:
    if not path.is_file():
        return {}
    aliases: dict[str, str | None] = {}
    with path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file, delimiter="\t"):
            raw_name = normalized_key(row.get("raw_name"))
            if not raw_name:
                continue
            aliases[raw_name] = clean_value(row.get("canonical_name"))
    return aliases


@dataclass(frozen=True)
class AuthorResolution:
    status: str
    raw_name: str | None
    canonical_name: str | None = None
    author: CatalogAuthor | None = None
    profile: "AuthorProfile | None" = None


@dataclass(frozen=True)
class AuthorProfile:
    canonical_name: str
    wikidata: str | None = None
    born: str | None = None
    died: str | None = None
    wikipedia: str | None = None


def load_author_profiles(path: Path) -> dict[str, AuthorProfile]:
    if not path.is_file():
        return {}
    profiles: dict[str, AuthorProfile] = {}
    with path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file, delimiter="\t"):
            canonical_name = clean_value(row.get("canonical_name"))
            if not canonical_name:
                continue
            profile = AuthorProfile(
                canonical_name=canonical_name,
                wikidata=clean_value(row.get("wikidata")),
                born=clean_value(row.get("born")),
                died=clean_value(row.get("died")),
                wikipedia=clean_value(row.get("wikipedia")),
            )
            profiles[normalized_key(canonical_name)] = profile
    return profiles


def resolve_author(
    raw_name: str | None,
    snapshot: CatalogSnapshot,
    aliases: dict[str, str | None],
    profiles: dict[str, AuthorProfile] | None = None,
) -> AuthorResolution:
    raw_name = clean_value(raw_name)
    raw_key = normalized_key(raw_name)
    if not raw_name or is_non_person_author(raw_name):
        return AuthorResolution("ignored", raw_name)

    cleaned_name = canonical_author_name(raw_name)
    canonical_name = aliases.get(raw_key)
    if canonical_name is None and raw_key not in aliases:
        canonical_name = aliases.get(normalized_key(cleaned_name), cleaned_name)
    if canonical_name is None:
        return AuthorResolution("ignored", raw_name)

    candidates = snapshot.authors_by_key().get(normalized_key(canonical_name), [])
    if len(candidates) == 1:
        status = "alias-matched" if normalized_key(canonical_name) != raw_key else "matched"
        return AuthorResolution(status, raw_name, canonical_name, candidates[0])
    if len(candidates) > 1:
        return AuthorResolution("ambiguous", raw_name, canonical_name)
    profile = (profiles or {}).get(normalized_key(canonical_name))
    if profile is not None:
        if profile.wikidata:
            qid_candidates = snapshot.authors_by_wikidata().get(
                profile.wikidata.casefold(), []
            )
            if len(qid_candidates) == 1:
                return AuthorResolution(
                    "alias-matched",
                    raw_name,
                    canonical_name,
                    qid_candidates[0],
                    profile,
                )
            if len(qid_candidates) > 1:
                return AuthorResolution("ambiguous", raw_name, canonical_name, profile=profile)
        return AuthorResolution("reviewed", raw_name, canonical_name, profile=profile)
    return AuthorResolution("unmatched", raw_name, canonical_name)


@dataclass
class Analysis:
    rows: int = 0
    importable_rows: int = 0
    skipped_by_author_policy: int = 0
    accepted_authors: Counter = field(default_factory=Counter)
    new_authors: Counter = field(default_factory=Counter)
    author_occurrences: Counter = field(default_factory=Counter)
    author_unique: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    unmatched_authors: Counter = field(default_factory=Counter)
    ambiguous_authors: Counter = field(default_factory=Counter)
    matched_genres: Counter = field(default_factory=Counter)
    unmatched_genres: Counter = field(default_factory=Counter)
    matched_tags: Counter = field(default_factory=Counter)
    unmatched_tags: Counter = field(default_factory=Counter)
    matched_instruments: Counter = field(default_factory=Counter)
    unmatched_instruments: Counter = field(default_factory=Counter)
    licenses: Counter = field(default_factory=Counter)


def analyze_rows(
    rows: Iterable[dict[str, str]],
    snapshot: CatalogSnapshot,
    aliases: dict[str, str | None],
    profiles: dict[str, AuthorProfile] | None = None,
    author_policy: str = "verified",
    importable_limit: int | None = None,
) -> Analysis:
    result = Analysis()
    for row in rows:
        if (
            importable_limit is not None
            and result.importable_rows >= importable_limit
        ):
            break
        result.rows += 1
        resolution = resolve_author(row.get("composer_name"), snapshot, aliases, profiles)
        if author_status_allowed(resolution.status, author_policy):
            result.importable_rows += 1
            accepted_name = (
                resolution.author.name
                if resolution.author is not None
                else resolution.profile.canonical_name
                if resolution.profile is not None
                else resolution.canonical_name
            )
            if accepted_name:
                result.accepted_authors[accepted_name] += 1
            if resolution.status == "reviewed" and resolution.profile is not None:
                result.new_authors[resolution.profile.canonical_name] += 1
        else:
            result.skipped_by_author_policy += 1
        result.author_occurrences[resolution.status] += 1
        if resolution.raw_name:
            result.author_unique[resolution.status].add(resolution.raw_name)
        if resolution.status == "unmatched" and resolution.raw_name:
            result.unmatched_authors[resolution.raw_name] += 1
        if resolution.status == "ambiguous" and resolution.raw_name:
            result.ambiguous_authors[resolution.raw_name] += 1
        if not author_status_allowed(resolution.status, author_policy):
            continue

        for value in split_metadata_values(row.get("genres")):
            slug = slug_key(value)
            target = result.matched_genres if slug in snapshot.genres else result.unmatched_genres
            target[slug] += 1
            if slug in TAG_WHITELIST:
                tag_target = (
                    result.matched_tags
                    if slug in snapshot.tags
                    else result.unmatched_tags
                )
                tag_target[slug] += 1

        for value in split_metadata_values(row.get("tags")):
            slug = slug_key(value)
            if slug not in TAG_WHITELIST:
                continue
            target = result.matched_tags if slug in snapshot.tags else result.unmatched_tags
            target[slug] += 1

        for candidates in instrument_candidate_groups(row):
            matched_slug = _existing_candidate(snapshot.instruments, candidates)
            if matched_slug:
                result.matched_instruments[matched_slug] += 1
            else:
                result.unmatched_instruments[candidates[0]] += 1

        license_name, _ = map_license(row)
        result.licenses[license_name or "missing"] += 1
    return result


def print_analysis(
    analysis: Analysis,
    snapshot: CatalogSnapshot,
    subset: str,
    top: int,
) -> None:
    print(f"DRY-RUN; subset={subset}; rows={analysis.rows}")
    print(
        f"author policy: importable={analysis.importable_rows}; "
        f"skipped={analysis.skipped_by_author_policy}"
    )
    print(
        "catalog: authors={authors}, tags={tags}, genres={genres}, instruments={instruments}".format(
            authors=len(snapshot.authors),
            tags=len(snapshot.tags),
            genres=len(snapshot.genres),
            instruments=len(snapshot.instruments),
        )
    )
    print("authors (score occurrences / unique raw values):")
    for status in (
        "matched",
        "alias-matched",
        "reviewed",
        "ignored",
        "ambiguous",
        "unmatched",
    ):
        print(
            f"  {status}: {analysis.author_occurrences[status]} / "
            f"{len(analysis.author_unique.get(status, set()))}"
        )
    if analysis.unmatched_authors:
        print(f"top {top} unmatched composers:")
        for name, count in analysis.unmatched_authors.most_common(top):
            print(f"  {count:>6}  {name}")
    if analysis.accepted_authors:
        print(f"top {top} accepted composers:")
        for name, count in analysis.accepted_authors.most_common(top):
            print(f"  {count:>6}  {name}")
    if analysis.new_authors:
        print(
            f"new reviewed author entities: {len(analysis.new_authors)} "
            "(--create-authors required in apply mode)"
        )
        for name, count in analysis.new_authors.most_common(top):
            print(f"  {count:>6}  {name}")
    if analysis.ambiguous_authors:
        print(f"top {top} ambiguous composers:")
        for name, count in analysis.ambiguous_authors.most_common(top):
            print(f"  {count:>6}  {name}")

    for label, matched, unmatched in (
        ("genres", analysis.matched_genres, analysis.unmatched_genres),
        ("tags", analysis.matched_tags, analysis.unmatched_tags),
        ("instruments", analysis.matched_instruments, analysis.unmatched_instruments),
    ):
        print(
            f"{label}: matched occurrences={sum(matched.values())}; "
            f"unmatched occurrences={sum(unmatched.values())}"
        )
        if unmatched:
            values = ", ".join(f"{name}={count}" for name, count in unmatched.most_common(top))
            print(f"  unmatched: {values}")
    print("licenses: " + ", ".join(f"{k}={v}" for k, v in analysis.licenses.items()))


class _LocalUpload:
    def __init__(self, path: Path, filename: str) -> None:
        self.file = path.open("rb")
        self.filename = filename

    def close(self) -> None:
        self.file.close()


class _BytesUpload:
    def __init__(self, content: bytes, filename: str) -> None:
        self.file = io.BytesIO(content)
        self.filename = filename


def _existing_candidate(slugs: set[str], candidates: Iterable[str]) -> str | None:
    return next((candidate for candidate in candidates if candidate in slugs), None)


def _score_format(n_tracks: str | None, instrument_slugs: set[str], tag_slugs: set[str]):
    from api.catalog_models import ScoreFormat

    if "orchestra" in tag_slugs:
        return ScoreFormat.orchestra
    if "choir" in instrument_slugs or "satb" in tag_slugs:
        return ScoreFormat.choir
    try:
        count = int(n_tracks or "")
    except ValueError:
        return None
    if count == 1:
        return ScoreFormat.solo
    if count == 2:
        return ScoreFormat.duet
    if count >= 3:
        return ScoreFormat.ensemble
    return None


def _attach_cover(db, score, source_id: str) -> bool:
    if score.cover or not score.music_file:
        return False
    from api.preview import render_cover_png

    db.refresh(score, attribute_names=["music_file"])
    music_path = Path(str(score.music_file))
    if not music_path.is_file():
        return False
    png = render_cover_png(music_path)
    if not png:
        return False
    score.cover = _BytesUpload(png, f"pdmx-{source_id}.png")
    return True


def _db_author_resolution(
    raw_name,
    authors_by_key,
    authors_by_wikidata,
    aliases,
    profiles,
):
    raw_name = clean_value(raw_name)
    raw_key = normalized_key(raw_name)
    if not raw_name or is_non_person_author(raw_name):
        return "ignored", raw_name, None, None
    cleaned_name = canonical_author_name(raw_name)
    canonical = aliases.get(raw_key)
    if canonical is None and raw_key not in aliases:
        canonical = aliases.get(normalized_key(cleaned_name), cleaned_name)
    if canonical is None:
        return "ignored", raw_name, None, None
    candidates = authors_by_key.get(normalized_key(canonical), [])
    if len(candidates) == 1:
        return "matched", canonical, candidates[0], None
    if len(candidates) > 1:
        return "ambiguous", canonical, None, None
    profile = profiles.get(normalized_key(canonical))
    if profile is not None:
        if profile.wikidata:
            qid_candidates = authors_by_wikidata.get(profile.wikidata.casefold(), [])
            if len(qid_candidates) == 1:
                return "matched", canonical, qid_candidates[0], profile
            if len(qid_candidates) > 1:
                return "ambiguous", canonical, None, profile
        return "reviewed", canonical, None, profile
    return "unmatched", canonical, None, None


def author_status_allowed(status: str, policy: str) -> bool:
    if policy == "all":
        return True
    if status in {"matched", "alias-matched", "reviewed"}:
        return True
    return policy == "verified-or-anonymous" and status == "ignored"


def apply_import(
    args,
    aliases: dict[str, str | None],
    profiles: dict[str, AuthorProfile],
) -> None:
    if args.attach_mxl and args.pdmx_dir is None:
        raise SystemExit("--attach-mxl requires --pdmx-dir")

    import httpx
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from api.catalog_enums import Difficulty
    from api.catalog_models import Author, Genre, Instrument, Score, SourceType, Tag
    from api.db import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    photo_client = None
    if args.photos:
        photo_client = httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": "audiveris-catalog-importer/1.0 (dev@dev.com)"},
        )

    counters = Counter()
    try:
        authors = db.execute(select(Author).order_by(Author.id)).scalars().all()
        authors_by_key = defaultdict(list)
        authors_by_wikidata = defaultdict(list)
        for author in authors:
            authors_by_key[normalized_key(author.name)].append(author)
            if author.wikidata:
                authors_by_wikidata[author.wikidata.casefold()].append(author)

        tags_by_slug = {tag.slug: tag for tag in db.execute(select(Tag)).scalars().all()}
        genres_by_slug = {
            genre.slug: genre for genre in db.execute(select(Genre)).scalars().all()
        }
        instruments_by_slug = {
            instrument.slug: instrument
            for instrument in db.execute(select(Instrument)).scalars().all()
        }
        existing_scores = db.execute(
            select(Score)
            .where(Score.source == SourceType.pdmx)
            .options(
                selectinload(Score.tags),
                selectinload(Score.genres),
                selectinload(Score.instruments),
            )
        ).scalars().all()
        scores_by_source_id = {str(score.source_id): score for score in existing_scores}

        photographed_author_ids: set[int] = set()
        for row in iter_selected_rows(args.csv, args.subset):
            if args.limit is not None and counters["rows"] >= args.limit:
                break
            status, canonical, author, profile = _db_author_resolution(
                row.get("composer_name"),
                authors_by_key,
                authors_by_wikidata,
                aliases,
                profiles,
            )
            if not author_status_allowed(status, args.author_policy):
                counters["rows_skipped_by_author_policy"] += 1
                continue
            if status == "reviewed" and not args.create_authors:
                counters["rows_skipped_missing_reviewed_author"] += 1
                continue

            sid = pdmx_source_id(row)
            score = scores_by_source_id.get(sid)
            is_new = score is None
            if is_new:
                score = Score(
                    source=SourceType.pdmx,
                    source_id=sid,
                    is_published=args.publish,
                )
                db.add(score)
                scores_by_source_id[sid] = score
                counters["scores_created"] += 1
            else:
                counters["scores_updated"] += 1

            if args.publish and not score.is_published:
                score.is_published = True
                counters["scores_published"] += 1

            title = clean_value(row.get("title")) or clean_value(row.get("song_name"))
            score.title = (title or "Untitled")[:512]
            score.source_url = PDMX_RECORD_URL
            score.license, score.license_url = map_license(row)
            score.source_metadata = source_metadata(row)
            difficulty = map_difficulty(row.get("complexity"))
            score.difficulty = Difficulty(difficulty) if difficulty else None

            counters[f"authors_{status}"] += 1
            if author is None and status == "reviewed" and args.create_authors:
                if profile is not None and looks_like_person(profile.canonical_name):
                    author = Author(
                        name=profile.canonical_name[:255],
                        born=profile.born,
                        died=profile.died,
                        wikidata=profile.wikidata,
                        wikipedia=profile.wikipedia,
                        source=SourceType.pdmx,
                        source_id=author_source_id(profile.canonical_name),
                        source_url=PDMX_RECORD_URL,
                    )
                    db.add(author)
                    db.flush()
                    authors_by_key[normalized_key(profile.canonical_name)].append(author)
                    if profile.wikidata:
                        authors_by_wikidata[profile.wikidata.casefold()].append(author)
                    counters["authors_created"] += 1
            if author is not None:
                score.author = author

            desired_genres = {slug_key(value) for value in split_metadata_values(row.get("genres"))}
            for slug in desired_genres:
                genre = genres_by_slug.get(slug)
                if genre is not None and genre not in score.genres:
                    score.genres.append(genre)
                    counters["genre_links_added"] += 1

            desired_tags = {
                slug_key(value)
                for value in split_metadata_values(row.get("tags"))
                if slug_key(value) in TAG_WHITELIST
            }
            desired_tags.update(slug for slug in desired_genres if slug in TAG_WHITELIST)
            for slug in desired_tags:
                tag = tags_by_slug.get(slug)
                if tag is None and args.create_terms and slug in TAG_DISPLAY_NAMES:
                    tag = Tag(name=TAG_DISPLAY_NAMES[slug], slug=slug)
                    db.add(tag)
                    db.flush()
                    tags_by_slug[slug] = tag
                    counters["tags_created"] += 1
                if tag is not None and tag not in score.tags:
                    score.tags.append(tag)
                    counters["tag_links_added"] += 1

            resolved_instrument_slugs: set[str] = set()
            for candidates in instrument_candidate_groups(row):
                slug = _existing_candidate(set(instruments_by_slug), candidates)
                if slug is None and args.create_terms:
                    slug = next(
                        (
                            candidate
                            for candidate in candidates
                            if candidate in INSTRUMENT_DISPLAY_NAMES
                        ),
                        None,
                    )
                    if slug is not None:
                        instrument = Instrument(
                            name=INSTRUMENT_DISPLAY_NAMES[slug],
                            slug=slug,
                        )
                        db.add(instrument)
                        db.flush()
                        instruments_by_slug[slug] = instrument
                        counters["instruments_created"] += 1
                if not slug:
                    continue
                resolved_instrument_slugs.add(slug)
                instrument = instruments_by_slug[slug]
                if instrument not in score.instruments:
                    score.instruments.append(instrument)
                    counters["instrument_links_added"] += 1

            score.format = _score_format(
                row.get("n_tracks"), resolved_instrument_slugs, desired_tags
            )

            upload = None
            if args.attach_mxl and not score.music_file:
                relative = (clean_value(row.get("mxl")) or "").removeprefix("./")
                mxl_path = args.pdmx_dir / relative
                if mxl_path.is_file():
                    upload = _LocalUpload(mxl_path, f"pdmx-{sid}.mxl")
                    score.music_file = upload
                    counters["mxl_attached"] += 1
                else:
                    counters["mxl_missing"] += 1

            try:
                db.flush()
            finally:
                if upload is not None:
                    upload.close()
            if args.covers and _attach_cover(db, score, sid):
                counters["covers_created"] += 1

            if (
                args.photos
                and author is not None
                and author.id not in photographed_author_ids
                and not author.photo
                and author.wikidata
            ):
                from import_lieder import _fetch_photo

                photographed_author_ids.add(author.id)
                if _fetch_photo(photo_client, author, "", author.wikidata):
                    counters["photos_created"] += 1

            counters["rows"] += 1
            if counters["rows"] % args.commit_every == 0:
                db.commit()
                print(f"  ...{counters['rows']} rows")

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if photo_client is not None:
            photo_client.close()
        db.close()

    print("APPLIED")
    for key in sorted(counters):
        print(f"  {key}: {counters[key]}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Импорт PDMX в каталог нот")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Путь к PDMX.csv")
    parser.add_argument(
        "--catalog-export",
        type=Path,
        default=DEFAULT_EXPORT,
        help="JSON-выгрузка справочников прода для offline dry-run",
    )
    parser.add_argument("--aliases", type=Path, default=DEFAULT_ALIASES)
    parser.add_argument("--author-profiles", type=Path, default=DEFAULT_AUTHOR_PROFILES)
    parser.add_argument(
        "--subset",
        choices=tuple(SUBSET_COLUMNS),
        default="rated-deduplicated",
    )
    parser.add_argument(
        "--author-policy",
        choices=("verified", "verified-or-anonymous", "all"),
        default="verified",
        help=(
            "verified — только существующие/проверенные исторические авторы; "
            "verified-or-anonymous — также anonymous/traditional; all — все строки"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Максимальное число строк, прошедших выбранную политику авторов",
    )
    parser.add_argument("--top", type=int, default=25, help="Размер top в dry-run отчёте")
    parser.add_argument("--apply", action="store_true", help="Записать изменения в БД")
    parser.add_argument(
        "--create-authors",
        action="store_true",
        help="Создавать только подтверждённых авторов из --author-profiles",
    )
    parser.add_argument(
        "--create-terms",
        action="store_true",
        help="Создать отсутствующие термины только из безопасных whitelist-списков",
    )
    parser.add_argument("--pdmx-dir", type=Path, default=None)
    parser.add_argument("--attach-mxl", action="store_true")
    parser.add_argument("--covers", action="store_true")
    parser.add_argument("--photos", action="store_true")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Сразу публиковать новые ноты; по умолчанию создаются черновики",
    )
    parser.add_argument("--commit-every", type=int, default=100)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.top < 1:
        parser.error("--top must be >= 1")
    if args.commit_every < 1:
        parser.error("--commit-every must be >= 1")
    if args.covers and not args.attach_mxl and args.pdmx_dir is not None:
        # Обложки могут быть дозалиты для уже импортированных MXL; это лишь
        # предупреждение для самого частого ошибочного запуска.
        print("Note: --covers without --attach-mxl processes only existing music_file values")
    return args


def main() -> None:
    args = parse_args()
    if not args.csv.is_file():
        raise SystemExit(f"PDMX CSV not found: {args.csv}")
    aliases = load_aliases(args.aliases)
    profiles = load_author_profiles(args.author_profiles)
    if args.apply:
        apply_import(args, aliases, profiles)
        return

    if args.catalog_export.is_file():
        snapshot = CatalogSnapshot.from_json(args.catalog_export)
    else:
        snapshot = CatalogSnapshot()
        print(
            f"Warning: catalog export not found: {args.catalog_export}; "
            "author/term matches will be reported as unmatched",
            file=sys.stderr,
        )
    analysis = analyze_rows(
        iter_selected_rows(args.csv, args.subset),
        snapshot,
        aliases,
        profiles,
        args.author_policy,
        args.limit,
    )
    print_analysis(analysis, snapshot, args.subset, args.top)


if __name__ == "__main__":
    main()
