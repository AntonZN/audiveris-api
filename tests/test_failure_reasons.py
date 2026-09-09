"""Тесты классификатора причин провала.

Главный тест здесь — `test_every_message_the_code_can_produce`: в нём собраны
ДОСЛОВНЫЕ сообщения, которые умеет выдать наш код. Классификатор работает по
тексту, и это его единственная слабость: поменяли формулировку — причина тихо
станет «не определена». Тест держит формулировки и классификатор вместе.
"""

from __future__ import annotations

import unittest

from api.failure_reasons import LABELS, FailureReason, classify, hint, label


class ClassifyTest(unittest.TestCase):
    def test_every_message_the_code_can_produce(self) -> None:
        cases = {
            # api/omr_bridge.py — мост пайплайна
            "Пайплайн omr не дал MusicXML: нет страниц с нотами":
                FailureReason.NO_MUSIC,
            "Пайплайн omr не дал MusicXML: нотных станов не найдено":
                FailureReason.NO_MUSIC,
            "Пайплайн omr не дал MusicXML: нотных станов не найдено; "
            "homr не дал MusicXML (код 1)":
                FailureReason.ENGINE_FAILED,
            "Пайплайн omr не смог обработать файл: не могу прочитать page.heic: "
            "cannot identify image file. Для HEIC/HEIF нужен пакет pillow-heif.":
                FailureReason.UNREADABLE_INPUT,
            "Пайплайн omr упал на page.png: ValueError: что-то пошло не так":
                FailureReason.INTERNAL_ERROR,
            # omr/engines/homr_engine.py
            "Пайплайн omr не дал MusicXML: homr не уложился в таймаут (180 c)":
                FailureReason.TIMEOUT,
            # api/homr_service.py — прежний путь, homr на сыром файле
            "homr не смог распознать фото (нет MusicXML на выходе)":
                FailureReason.ENGINE_FAILED,
            "homr timed out after 180 seconds":
                FailureReason.TIMEOUT,
            # api/services.py — гейты постобработки
            "Распознавание не нашло музыки: 2 нот в выходе (порог 3). Похоже, на "
            "входе не партитура (скриншот/фото без нот) или качество слишком низкое.":
                FailureReason.NO_MUSIC,
            "Не удалось получить валидный MusicXML: verovio не принимает выход "
            "движка ни после music21-фикса, ни после удаления проблемных тактов":
                FailureReason.INVALID_MUSICXML,
            "Файл score.sib не берёт ни один движок: omr выключен или не знает "
            "такой формат, а homr читает только растр":
                FailureReason.UNSUPPORTED_FORMAT,
            "В плейлисте есть файлы, которые не берёт ни один движок: score.sib":
                FailureReason.UNSUPPORTED_FORMAT,
            "Не удалось склеить страницы плейлиста: list index out of range":
                FailureReason.MERGE_FAILED,
            # Масштаб стана: корневая причина, найдена на боевом файле
            # (крупный план одного стана, 186px на интервал)
            "Пайплайн omr не дал MusicXML: стан слишком крупный (186.5px на интервал); "
            "homr не дал MusicXML (код 1)":
                FailureReason.BAD_SCALE,
            "Пайплайн omr не дал MusicXML: стан слишком мелкий (4.2px на интервал)":
                FailureReason.BAD_SCALE,
            # omr/stages/load.py — через обёртку моста
            "Пайплайн omr не смог обработать файл: файла нет: /data/in/page.png":
                FailureReason.UNREADABLE_INPUT,
        }
        for message, expected in cases.items():
            with self.subTest(message=message[:60]):
                self.assertEqual(classify(message), expected.value)

    def test_engine_failure_wins_over_empty_pages(self) -> None:
        """Составное сообщение: часть страниц без нот, на части упал движок.
        Падение движка — это к нам, и в статистике оно важнее пустых страниц."""
        self.assertEqual(
            classify("нотных станов не найдено; homr не дал MusicXML (код 1)"),
            FailureReason.ENGINE_FAILED.value,
        )

    def test_bridge_prefix_alone_does_not_mean_the_engine_failed(self) -> None:
        """«Пайплайн omr не дал MusicXML» — префикс ЛЮБОГО провала пайплайна.
        Классифицировать по нему значит записать все провалы в падения движка."""
        self.assertEqual(
            classify("Пайплайн omr не дал MusicXML: нет страниц с нотами"),
            FailureReason.NO_MUSIC.value,
        )

    def test_scale_beats_the_engine_it_broke(self) -> None:
        """«Движок не справился» на таком кадре — следствие, а не причина: при
        интервале вне рабочего коридора движок не заработает при любом перезапуске."""
        self.assertEqual(
            classify("стан слишком крупный (186.5px на интервал); "
                     "homr не дал MusicXML (код 1)"),
            FailureReason.BAD_SCALE.value,
        )

    def test_unknown_for_empty_and_foreign_text(self) -> None:
        for text in [None, "", "   ", "что-то совсем новое"]:
            with self.subTest(text=text):
                self.assertEqual(classify(text), FailureReason.UNKNOWN.value)

    def test_every_reason_has_a_label_and_a_hint(self) -> None:
        """Причина без подписи в админке — просто строка кода на экране."""
        for reason in FailureReason:
            with self.subTest(reason=reason.value):
                self.assertIn(reason, LABELS)
                self.assertTrue(label(reason.value))
                self.assertTrue(hint(reason.value))

    def test_label_survives_an_unknown_code(self) -> None:
        """Старое или чужое значение в БД не должно ронять страницу админки."""
        self.assertEqual(label("что-то_из_будущего"), "что-то_из_будущего")
        self.assertEqual(label(None), LABELS[FailureReason.UNKNOWN][0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
