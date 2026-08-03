-- Безопасная read-only выгрузка справочников для настройки PDMX-импорта.
-- Пользователи, рейтинги и пути к медиафайлам в результат не попадают.
SELECT jsonb_pretty(
    jsonb_build_object(
        'authors', (
            SELECT coalesce(jsonb_agg(to_jsonb(row_data)), '[]'::jsonb)
            FROM (
                SELECT
                    authors.id,
                    authors.name,
                    authors.slug,
                    authors.born,
                    authors.died,
                    authors.wikidata,
                    authors.wikipedia,
                    authors.source,
                    authors.source_id,
                    count(scores.id) AS scores_count
                FROM authors
                LEFT JOIN scores ON scores.author_id = authors.id
                GROUP BY authors.id
                ORDER BY authors.id
            ) AS row_data
        ),
        'tags', (
            SELECT coalesce(jsonb_agg(to_jsonb(row_data)), '[]'::jsonb)
            FROM (
                SELECT
                    tags.id,
                    tags.name,
                    tags.slug,
                    count(score_tags.score_id) AS scores_count
                FROM tags
                LEFT JOIN score_tags ON score_tags.tag_id = tags.id
                GROUP BY tags.id
                ORDER BY tags.id
            ) AS row_data
        ),
        'genres', (
            SELECT coalesce(jsonb_agg(to_jsonb(row_data)), '[]'::jsonb)
            FROM (
                SELECT
                    genres.id,
                    genres.name,
                    genres.slug,
                    count(score_genres.score_id) AS scores_count
                FROM genres
                LEFT JOIN score_genres ON score_genres.genre_id = genres.id
                GROUP BY genres.id
                ORDER BY genres.id
            ) AS row_data
        ),
        'instruments', (
            SELECT coalesce(jsonb_agg(to_jsonb(row_data)), '[]'::jsonb)
            FROM (
                SELECT
                    instruments.id,
                    instruments.name,
                    instruments.slug,
                    count(score_instruments.score_id) AS scores_count
                FROM instruments
                LEFT JOIN score_instruments
                    ON score_instruments.instrument_id = instruments.id
                GROUP BY instruments.id
                ORDER BY instruments.id
            ) AS row_data
        )
    )
);
