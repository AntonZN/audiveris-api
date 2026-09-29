from __future__ import annotations

import unittest

from api.catalog_schemas import InstrumentListItem, SoundFontOut
from api.localization import parse_rows, with_default_en


class ParseRowsTest(unittest.TestCase):
    def test_rows_become_dict_in_input_order(self) -> None:
        data, errors = parse_rows([("en", "Piano"), ("ru", " Пианино "), ("", "")])

        self.assertEqual(errors, [])
        self.assertEqual(list(data.items()), [("en", "Piano"), ("ru", "Пианино")])

    def test_language_code_is_normalized(self) -> None:
        data, errors = parse_rows([("RU", "Пианино"), ("pt_BR", "Piano"), ("zh-Hans", "钢琴")])

        self.assertEqual(errors, [])
        self.assertEqual(list(data), ["ru", "pt-BR", "zh-Hans"])

    def test_invalid_rows_are_reported(self) -> None:
        data, errors = parse_rows(
            [
                ("ru", "Пианино"),
                ("", "Klavier"),
                ("de", ""),
                ("русский", "Пианино"),
                ("RU", "Фортепиано"),
            ]
        )

        self.assertEqual(data, {"ru": "Пианино"})
        self.assertEqual(len(errors), 4)
        self.assertIn("«ru» указан дважды", errors[-1])


class DefaultEnTest(unittest.TestCase):
    def test_en_is_taken_from_name_and_goes_first(self) -> None:
        self.assertEqual(
            list(with_default_en({"ru": "Пианино"}, "Piano").items()),
            [("en", "Piano"), ("ru", "Пианино")],
        )

    def test_explicit_en_is_kept(self) -> None:
        self.assertEqual(with_default_en({"en": "Grand piano"}, "Piano"), {"en": "Grand piano"})

    def test_empty(self) -> None:
        self.assertEqual(with_default_en(None, "Piano"), {"en": "Piano"})


class PayloadShapeTest(unittest.TestCase):
    def test_instrument_has_localization(self) -> None:
        item = InstrumentListItem(
            id=1,
            name="Piano",
            slug="piano",
            localization={"en": "Piano", "ru": "Пианино"},
        )

        payload = item.model_dump(by_alias=True)

        self.assertEqual(payload["localization"], {"en": "Piano", "ru": "Пианино"})

    def test_soundfont_payload_is_camel_case(self) -> None:
        item = SoundFontOut(
            id=1,
            name="Grand Piano",
            preview_url="https://media.example/catalog/grand-piano.mp3",
            download_url="https://media.example/catalog/grand-piano.sf2",
            localization={"en": "Grand Piano", "ru": "Рояль"},
        )

        payload = item.model_dump(by_alias=True)

        self.assertEqual(
            set(payload), {"id", "name", "previewUrl", "downloadUrl", "localization"}
        )
        self.assertEqual(payload["downloadUrl"], "https://media.example/catalog/grand-piano.sf2")


if __name__ == "__main__":
    unittest.main()
