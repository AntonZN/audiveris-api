from __future__ import annotations

import unittest

from scripts.import_pdmx import (
    AuthorProfile,
    CatalogAuthor,
    CatalogSnapshot,
    analyze_rows,
    author_status_allowed,
    author_source_id,
    canonical_author_name,
    desired_instrument_slugs,
    gm_instrument_candidates,
    is_non_person_author,
    map_difficulty,
    map_license,
    normalized_key,
    pdmx_source_id,
    resolve_author,
    row_selected,
    source_metadata,
)


class PdmxImportTest(unittest.TestCase):
    def test_row_selected_always_requires_safe_and_valid(self) -> None:
        row = {
            "subset:no_license_conflict": "True",
            "subset:all_valid": "True",
            "subset:rated_deduplicated": "True",
        }
        self.assertTrue(row_selected(row, "rated-deduplicated"))
        row["subset:no_license_conflict"] = "False"
        self.assertFalse(row_selected(row, "rated-deduplicated"))
        row["subset:no_license_conflict"] = "True"
        row["subset:all_valid"] = "False"
        self.assertFalse(row_selected(row, "rated-deduplicated"))

    def test_source_id_prefers_unique_musescore_metadata_id(self) -> None:
        row = {
            "metadata": "./metadata/5/5740212.json",
            "path": "./data/1/11/QmHash.json",
        }
        self.assertEqual(pdmx_source_id(row), "5740212")
        self.assertEqual(
            pdmx_source_id({"metadata": "NA", "path": "./data/1/11/QmHash.json"}),
            "QmHash",
        )

    def test_normalized_name_handles_diacritics_and_catalog_order(self) -> None:
        self.assertEqual(
            normalized_key("Händel, George Frideric"),
            normalized_key("George Frideric Handel"),
        )

    def test_canonical_author_name_removes_dates_and_opus(self) -> None:
        self.assertEqual(
            canonical_author_name("Francisco Tárrega (1852 - 1909)"),
            "Francisco Tárrega",
        )
        self.assertEqual(canonical_author_name("Edvard Grieg Op. 40"), "Edvard Grieg")
        self.assertEqual(canonical_author_name("by Scott Joplin"), "Scott Joplin")

    def test_author_resolution_is_conservative(self) -> None:
        snapshot = CatalogSnapshot(
            authors=[CatalogAuthor(id=7, name="Johann Sebastian Bach")]
        )
        aliases = {normalized_key("J. S. Bach"): "Johann Sebastian Bach"}
        resolved = resolve_author("J. S. Bach", snapshot, aliases)
        self.assertEqual(resolved.status, "alias-matched")
        self.assertEqual(resolved.author.id, 7)

        self.assertEqual(resolve_author("Trad.", snapshot, aliases).status, "ignored")
        self.assertEqual(resolve_author("Different Person", snapshot, aliases).status, "unmatched")

    def test_duplicate_catalog_names_are_ambiguous(self) -> None:
        snapshot = CatalogSnapshot(
            authors=[
                CatalogAuthor(id=1, name="John Smith"),
                CatalogAuthor(id=2, name="John Smith"),
            ]
        )
        self.assertEqual(resolve_author("John Smith", snapshot, {}).status, "ambiguous")

    def test_new_author_requires_reviewed_profile(self) -> None:
        profiles = {
            normalized_key("Johann Sebastian Bach"): AuthorProfile(
                canonical_name="Johann Sebastian Bach",
                wikidata="Q1339",
                born="1685",
                died="1750",
            )
        }
        resolution = resolve_author(
            "J. S. Bach",
            CatalogSnapshot(),
            {normalized_key("J. S. Bach"): "Johann Sebastian Bach"},
            profiles,
        )
        self.assertEqual(resolution.status, "reviewed")
        self.assertEqual(resolution.profile.wikidata, "Q1339")

        analysis = analyze_rows(
            [{"composer_name": "J. S. Bach"}],
            CatalogSnapshot(),
            {normalized_key("J. S. Bach"): "Johann Sebastian Bach"},
            profiles,
        )
        self.assertEqual(analysis.new_authors["Johann Sebastian Bach"], 1)

    def test_reviewed_profile_reuses_existing_wikidata_author(self) -> None:
        snapshot = CatalogSnapshot(
            authors=[
                CatalogAuthor(
                    id=9,
                    name="Different localized display name",
                    wikidata="Q1339",
                )
            ]
        )
        profiles = {
            normalized_key("Johann Sebastian Bach"): AuthorProfile(
                canonical_name="Johann Sebastian Bach",
                wikidata="Q1339",
            )
        }
        resolution = resolve_author(
            "J. S. Bach",
            snapshot,
            {normalized_key("J. S. Bach"): "Johann Sebastian Bach"},
            profiles,
        )
        self.assertEqual(resolution.status, "alias-matched")
        self.assertEqual(resolution.author.id, 9)

    def test_verified_policy_rejects_unmatched_and_anonymous(self) -> None:
        self.assertTrue(author_status_allowed("matched", "verified"))
        self.assertTrue(author_status_allowed("reviewed", "verified"))
        self.assertFalse(author_status_allowed("unmatched", "verified"))
        self.assertFalse(author_status_allowed("ignored", "verified"))
        self.assertTrue(author_status_allowed("ignored", "verified-or-anonymous"))
        self.assertTrue(author_status_allowed("unmatched", "all"))

    def test_non_person_values_are_ignored(self) -> None:
        for value in ("anon.", "Traditional", "Urheber unbekannt 1720 belegt", "Composer"):
            self.assertTrue(is_non_person_author(value), value)

    def test_general_midi_programs_map_to_catalog_instruments(self) -> None:
        self.assertEqual(gm_instrument_candidates(0), ("piano",))
        self.assertEqual(gm_instrument_candidates(40), ("violin",))
        self.assertIn("flute", gm_instrument_candidates(73))
        row = {"tracks": "0-40-73", "tags": "cello"}
        self.assertEqual(
            desired_instrument_slugs(row),
            {"piano", "violin", "flute", "cello"},
        )

    def test_license_and_difficulty_mapping(self) -> None:
        self.assertEqual(map_difficulty("0"), 1)
        self.assertEqual(map_difficulty("1"), 1)
        self.assertEqual(map_difficulty("2"), 2)
        self.assertEqual(map_difficulty("3"), 3)
        self.assertEqual(map_difficulty("NA"), None)
        self.assertEqual(
            map_license(
                {
                    "license": "cc-zero",
                    "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                }
            ),
            (
                "CC0 1.0",
                "https://creativecommons.org/publicdomain/zero/1.0/",
            ),
        )

    def test_source_metadata_keeps_license_audit_flags(self) -> None:
        metadata = source_metadata(
            {
                "license": "cc-zero",
                "license_conflict": "False",
                "subset:no_license_conflict": "True",
                "subset:all_valid": "True",
                "subset:rated_deduplicated": "True",
            }
        )
        self.assertEqual(metadata["dataset"], "PDMX")
        self.assertEqual(metadata["subset:no_license_conflict"], "True")
        self.assertEqual(metadata["subset:rated_deduplicated"], "True")

    def test_analysis_reuses_existing_terms_only(self) -> None:
        snapshot = CatalogSnapshot(
            authors=[CatalogAuthor(id=1, name="Johann Sebastian Bach")],
            tags={"classical", "easy"},
            genres={"classical"},
            instruments={"piano", "violin"},
        )
        row = {
            "composer_name": "J. S. Bach",
            "genres": "classical",
            "tags": "easy-piano",
            "tracks": "0-40",
            "license": "publicdomain",
            "license_url": "https://creativecommons.org/publicdomain/mark/1.0/",
        }
        aliases = {normalized_key("J. S. Bach"): "Johann Sebastian Bach"}
        analysis = analyze_rows([row], snapshot, aliases)
        self.assertEqual(analysis.author_occurrences["alias-matched"], 1)
        self.assertEqual(analysis.matched_genres["classical"], 1)
        self.assertEqual(analysis.matched_tags["classical"], 1)
        self.assertEqual(analysis.matched_tags["easy"], 1)
        self.assertEqual(analysis.matched_instruments["piano"], 1)
        self.assertEqual(analysis.matched_instruments["violin"], 1)

    def test_analysis_limit_counts_importable_rows(self) -> None:
        snapshot = CatalogSnapshot(
            authors=[CatalogAuthor(id=1, name="Johann Sebastian Bach")]
        )
        rows = [
            {"composer_name": "Unknown Person"},
            {"composer_name": "Johann Sebastian Bach"},
            {"composer_name": "Johann Sebastian Bach"},
        ]
        analysis = analyze_rows(
            rows,
            snapshot,
            {},
            importable_limit=1,
        )
        self.assertEqual(analysis.rows, 2)
        self.assertEqual(analysis.importable_rows, 1)
        self.assertEqual(analysis.skipped_by_author_policy, 1)

    def test_generated_author_source_id_is_stable(self) -> None:
        self.assertEqual(
            author_source_id("George Frideric Händel"),
            author_source_id("George Frideric Handel"),
        )


if __name__ == "__main__":
    unittest.main()
