#!/usr/bin/env python3
"""Проставить теги и difficulty нотам из OpenScore/Lieder.

Данные подготовлены по snapshot OpenScore/Lieder:
https://github.com/OpenScore/Lieder/commit/6b2dc542ce2e8aa4b78c8ee62103b210efc07015

`scripts/data/lieder_enrichment.tsv` содержит одну строку на `scores.tsv.id`,
связанный `set_id`, difficulty 1/2/3 и набор тегов.

Текущий `import_lieder.py` сохраняет `scores.tsv.id` в `Score.source_id`, поэтому
обычно скрипт обновляет ноты по score-id, предварительно используя set_id для
связи с циклом и композитором. Если в конкретной БД в `source_id` сохранён именно
set_id, режим auto обнаружит это и агрегирует данные всех песен набора.

По умолчанию выполняется dry-run. Для записи добавьте `--apply`:

    docker compose exec audiveris-api \
        python scripts/enrich_lieder_scores.py

    docker compose exec audiveris-api \
        python scripts/enrich_lieder_scores.py --apply

Существующие теги сохраняются, рассчитанные теги только добавляются. Difficulty
обновляется рассчитанным значением; `--only-missing-difficulty` ограничивает
обновление нотами, у которых difficulty ещё не заполнен.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# Чтобы `import api...` работал при запуске как ./scripts/enrich_lieder_scores.py.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, selectinload  # noqa: E402

from api.catalog_enums import Difficulty  # noqa: E402
from api.catalog_models import Score, SourceType, Tag  # noqa: E402
from api.db import SessionLocal, init_db  # noqa: E402


DATA_PATH = Path(__file__).resolve().parent / "data" / "lieder_enrichment.tsv"
EXPECTED_ROWS = 1356

# Имена, которые будут показаны в API/админке. Ключи совпадают со slug в TSV.
TAG_NAMES = {
    "american": "American",
    "art-song": "Art song",
    "austrian": "Austrian",
    "birds": "Birds",
    "bohemian": "Bohemian",
    "brazilian": "Brazilian",
    "british": "British",
    "children": "Children",
    "classical": "classical",
    "classical-period": "Classical period",
    "dance": "Dance",
    "dreams": "Dreams",
    "early-modern": "Early modern",
    "female-composer": "Female composer",
    "folk": "Folk",
    "french": "French",
    "german": "German",
    "hawaiian": "Hawaiian",
    "impressionism": "Impressionism",
    "irish": "Irish",
    "late-romantic": "Late Romantic",
    "love": "Love",
    "lullaby": "Lullaby",
    "modernism": "Modernism",
    "mourning": "Mourning",
    "nature": "Nature",
    "night": "Night",
    "ragtime": "Ragtime",
    "romantic": "Romantic",
    "sacred": "Sacred",
    "sea-and-water": "Sea and water",
    "seasons": "Seasons",
    "serenade": "Serenade",
    "song-cycle": "Song cycle",
    "spiritual": "Spiritual",
    "swedish": "Swedish",
    "travel": "Travel",
    "piano": "Piano",
    "war-and-patriotic": "War and patriotic",
}


@dataclass(frozen=True)
class Enrichment:
    score_id: str
    set_id: str
    difficulty: Difficulty
    tags: frozenset[str]


@dataclass
class UpdateStats:
    database_scores: int = 0
    matched: int = 0
    unmatched: int = 0
    difficulty_updated: int = 0
    tags_attached: int = 0
    tags_created: int = 0
    legacy_tags_deleted: int = 0


def load_enrichment(path: Path = DATA_PATH) -> list[Enrichment]:
    if not path.is_file():
        raise RuntimeError(f"Enrichment data not found: {path}")

    rows: list[Enrichment] = []
    seen_score_ids: set[str] = set()
    with path.open(encoding="utf-8", newline="") as file:
        for line_number, row in enumerate(
            csv.DictReader(file, delimiter="\t"),
            start=2,
        ):
            score_id = (row.get("score_id") or "").strip()
            set_id = (row.get("set_id") or "").strip()
            if not score_id or not set_id:
                raise RuntimeError(
                    f"Missing score_id/set_id in {path}:{line_number}"
                )
            if score_id in seen_score_ids:
                raise RuntimeError(
                    f"Duplicate score_id {score_id} in {path}:{line_number}"
                )
            seen_score_ids.add(score_id)

            try:
                difficulty = Difficulty(int(row["difficulty"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid difficulty in {path}:{line_number}"
                ) from exc

            tags = frozenset(
                slug.strip()
                for slug in (row.get("tags") or "").split(",")
                if slug.strip()
            )
            unknown_tags = tags.difference(TAG_NAMES)
            if unknown_tags:
                raise RuntimeError(
                    f"Unknown tags {sorted(unknown_tags)} in {path}:{line_number}"
                )
            rows.append(
                Enrichment(
                    score_id=score_id,
                    set_id=set_id,
                    difficulty=difficulty,
                    tags=tags,
                )
            )

    if len(rows) != EXPECTED_ROWS:
        raise RuntimeError(
            f"Expected {EXPECTED_ROWS} enrichment rows, found {len(rows)} in {path}"
        )
    return rows


def aggregate_by_set(rows: list[Enrichment]) -> dict[str, Enrichment]:
    grouped: dict[str, list[Enrichment]] = {}
    for row in rows:
        grouped.setdefault(row.set_id, []).append(row)

    result: dict[str, Enrichment] = {}
    for set_id, set_rows in grouped.items():
        # Для записи, представляющей весь set, берём медианную сложность цикла и
        # объединение тегов всех входящих произведений.
        median = statistics.median(row.difficulty.value for row in set_rows)
        difficulty = Difficulty(math.floor(median + 0.5))
        result[set_id] = Enrichment(
            score_id="",
            set_id=set_id,
            difficulty=difficulty,
            tags=frozenset().union(*(row.tags for row in set_rows)),
        )
    return result


def choose_match_mode(
    requested: str,
    scores: list[Score],
    by_score_id: dict[str, Enrichment],
    by_set_id: dict[str, Enrichment],
) -> str:
    if requested != "auto":
        return requested

    source_ids = {str(score.source_id) for score in scores if score.source_id}
    score_id_matches = len(source_ids.intersection(by_score_id))
    set_id_matches = len(source_ids.intersection(by_set_id))

    if score_id_matches == 0 and set_id_matches == 0:
        raise RuntimeError(
            "No OpenScore/Lieder source_id matches either scores.tsv.id or set_id"
        )
    return "set-id" if set_id_matches > score_id_matches else "score-id"


def ensure_tags(
    db: Session,
    required_slugs: set[str],
) -> tuple[dict[str, Tag], int]:
    existing = db.execute(
        select(Tag).where(Tag.slug.in_(sorted(required_slugs)))
    ).scalars().all()
    by_slug = {tag.slug: tag for tag in existing}
    created = 0

    for slug in sorted(required_slugs.difference(by_slug)):
        tag = Tag(name=TAG_NAMES[slug], slug=slug)
        db.add(tag)
        by_slug[slug] = tag
        created += 1
    if created:
        db.flush()
    return by_slug, created


def enrich_database(
    db: Session,
    rows: list[Enrichment],
    *,
    match_by: str = "auto",
    only_missing_difficulty: bool = False,
    limit: int | None = None,
) -> tuple[UpdateStats, str, Counter]:
    by_score_id = {row.score_id: row for row in rows}
    by_set_id = aggregate_by_set(rows)

    scores = db.execute(
        select(Score)
        .where(Score.source == SourceType.openscore_lieder)
        .order_by(Score.id)
        .options(selectinload(Score.tags))
    ).scalars().all()

    mode = choose_match_mode(match_by, scores, by_score_id, by_set_id)
    lookup = by_score_id if mode == "score-id" else by_set_id
    if limit is not None:
        scores = scores[:limit]

    matched_rows = [
        (score, lookup.get(str(score.source_id)))
        for score in scores
    ]
    required_slugs = set().union(
        *(
            enrichment.tags
            for _, enrichment in matched_rows
            if enrichment is not None
        )
    )
    tags_by_slug, tags_created = ensure_tags(db, required_slugs)

    stats = UpdateStats(
        database_scores=len(scores),
        tags_created=tags_created,
    )
    difficulty_counts: Counter = Counter()

    for score, enrichment in matched_rows:
        if enrichment is None:
            stats.unmatched += 1
            continue

        stats.matched += 1
        difficulty_counts[enrichment.difficulty.value] += 1
        if not only_missing_difficulty or score.difficulty is None:
            if score.difficulty != enrichment.difficulty:
                score.difficulty = enrichment.difficulty
                stats.difficulty_updated += 1

        existing_slugs = {tag.slug for tag in score.tags}
        for slug in sorted(enrichment.tags.difference(existing_slugs)):
            score.tags.append(tags_by_slug[slug])
            stats.tags_attached += 1

    # Ранняя версия snapshot использовала voice-and-piano. После перехода на
    # обычный piano удаляем устаревший справочный тег целиком; каскадная связь
    # уберёт его со всех нот. В dry-run это изменение также откатывается.
    legacy_tags = db.execute(
        select(Tag).where(Tag.slug == "voice-and-piano")
    ).scalars().all()
    for legacy_tag in legacy_tags:
        db.delete(legacy_tag)
        stats.legacy_tags_deleted += 1

    return stats, mode, difficulty_counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проставить OpenScore/Lieder теги и difficulty по source_id/set_id"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Записать изменения. Без флага выполняется dry-run с rollback.",
    )
    parser.add_argument(
        "--match-by",
        choices=("auto", "score-id", "set-id"),
        default="auto",
        help=(
            "Как сопоставлять Score.source_id: auto (по умолчанию), "
            "scores.tsv.id или set_id"
        ),
    )
    parser.add_argument(
        "--only-missing-difficulty",
        action="store_true",
        help="Не перезаписывать уже заполненный difficulty",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Обработать только первые N нот (для проверки)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be >= 1")

    # Гарантирует наличие таблиц tags/score_tags перед обновлением.
    init_db()
    rows = load_enrichment()

    with SessionLocal() as db:
        try:
            stats, mode, difficulty_counts = enrich_database(
                db,
                rows,
                match_by=args.match_by,
                only_missing_difficulty=args.only_missing_difficulty,
                limit=args.limit,
            )
            if args.apply:
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise

    action = "APPLIED" if args.apply else "DRY-RUN (rolled back)"
    print(f"{action}; match mode: {mode}")
    print(
        "database={database_scores}, matched={matched}, unmatched={unmatched}, "
        "difficulty updated={difficulty_updated}, tag links added={tags_attached}, "
        "tags created={tags_created}, legacy tags deleted={legacy_tags_deleted}"
        .format(**vars(stats))
    )
    print(
        "matched difficulty distribution: "
        + ", ".join(
            f"{value}={difficulty_counts[value]}" for value in (1, 2, 3)
        )
    )
    if stats.unmatched:
        print(
            "Warning: some OpenScore/Lieder rows were not found in enrichment data. "
            "Check --match-by and Score.source_id.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
