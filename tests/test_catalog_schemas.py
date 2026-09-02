from __future__ import annotations

import unittest

from api.catalog_schemas import ScoreListItem


class CatalogSchemaTest(unittest.TestCase):
    def test_score_list_item_exposes_camel_case_audio_url(self) -> None:
        item = ScoreListItem(
            id=1,
            title="Preview score",
            slug="preview-score",
            audio_url="https://media.example/preview.mp3",
        )

        payload = item.model_dump(by_alias=True)

        self.assertEqual(payload["audioUrl"], "https://media.example/preview.mp3")
        self.assertNotIn("audio_url", payload)


if __name__ == "__main__":
    unittest.main()
