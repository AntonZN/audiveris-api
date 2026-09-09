"""Тесты сводки провалов по причинам и записи причины в архив.

БД тут не поднимается: подставляется сессия-заглушка. Проверяется то, где и
живут ошибки такой сводки, — арифметика долей, порядок строк, пустой период — и
то, что причина вообще доезжает до строки архива (иначе метрика молча собирала бы
одни NULL).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from api import failure_stats, failures
from api.failure_reasons import FailureReason


class FakeSession:
    """Сессия, которая отдаёт заранее заданные строки и запоминает добавленные."""

    def __init__(self, rows: list | None = None) -> None:
        self.rows = rows or []
        self.added: list = []
        self.committed = False
        self.closed = False

    def execute(self, _statement):
        return SimpleResult(self.rows)

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


class SimpleResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class ByReasonTest(unittest.TestCase):
    def _stats(self, rows: list):
        session = FakeSession(rows)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        reasons, total = failure_stats.by_reason(cutoff, session_factory=lambda: session)
        self.assertTrue(session.closed, "сессию надо закрывать даже на пустом ответе")
        return reasons, total

    def test_counts_shares_and_orders_by_frequency(self) -> None:
        reasons, total = self._stats([
            (FailureReason.NO_MUSIC.value, 3),
            (FailureReason.ENGINE_FAILED.value, 6),
            (FailureReason.TIMEOUT.value, 1),
        ])

        self.assertEqual(total, 10)
        self.assertEqual([r["code"] for r in reasons],
                         ["engine_failed", "no_music", "timeout"])
        self.assertEqual([r["share"] for r in reasons], [60, 30, 10])
        self.assertEqual(reasons[0]["label"], "Движок не справился")
        self.assertTrue(reasons[0]["hint"])

    def test_null_reason_becomes_unknown(self) -> None:
        """Строки, записанные до введения причин, из сводки выпадать не должны."""
        reasons, total = self._stats([(None, 2)])

        self.assertEqual(total, 2)
        self.assertEqual(reasons[0]["code"], "unknown")
        self.assertEqual(reasons[0]["share"], 100)

    def test_empty_period_does_not_divide_by_zero(self) -> None:
        self.assertEqual(self._stats([]), ([], 0))


class RecordFailureTest(unittest.TestCase):
    def test_stores_the_reason_next_to_the_error(self) -> None:
        """Провал доезжает до архива уже классифицированным."""
        session = FakeSession()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "page.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\n")

            with mock.patch.object(failures, "SessionLocal", lambda: session), \
                 mock.patch.object(failures.settings, "failures_dir", tmp):
                failures.record_failure(
                    task_id="t1", kind="single", preset=None, enhance=False,
                    input_paths=[source],
                    error="Пайплайн omr не дал MusicXML: нотных станов не найдено; "
                          "homr не дал MusicXML (код 1)",
                )

        self.assertEqual(len(session.added), 1)
        row = session.added[0]
        self.assertEqual(row.reason, FailureReason.ENGINE_FAILED.value)
        self.assertEqual(row.filename, "page.png")
        self.assertTrue(session.committed)

    def test_archives_the_processing_log_next_to_the_file(self) -> None:
        """В архиве должен оказаться и отчёт пайплайна: по нему разбирают ПОЧЕМУ,
        а во временном output_dir он живёт только до уборки по TTL."""
        session = FakeSession()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "in" / "page.png"
            source.parent.mkdir()
            source.write_bytes(b"\x89PNG\r\n\x1a\n")

            out = root / "out"
            (out / "page.pages").mkdir(parents=True)
            (out / "page.omr.txt").write_text("отчёт стадий")
            (out / "page.engine.log").write_text("вывод движка")
            (out / "page.pages" / "page.p02.engine.log").write_text("вторая страница")

            with mock.patch.object(failures, "SessionLocal", lambda: session), \
                 mock.patch.object(failures.settings, "failures_dir", str(root / "arch")):
                failures.record_failure(
                    task_id="t2", kind="single", preset=None, enhance=False,
                    input_paths=[source], error="homr не дал MusicXML (код 1)",
                    output_dir=out,
                )

            row = session.added[0]
            self.assertIsNotNone(row.log_path)
            # Отчёт пайплайна информативнее лога движка — он и должен быть первым.
            self.assertTrue(row.log_path.endswith("page.omr.txt"), row.log_path)
            self.assertTrue(Path(row.log_path).exists())
            logs = sorted(path.name for path in (root / "arch" / "t2" / "logs").iterdir())
            self.assertEqual(
                logs, ["page.engine.log", "page.omr.txt", "page.pages_page.p02.engine.log"],
                "имя копии должно нести путь: одноимённые логи страниц затрут друг друга",
            )

    def test_archives_the_frame_that_went_into_the_engine(self) -> None:
        """Кадр после подготовки — главный экспонат разбора: по логу видно
        «станов 0», а по нему — что подготовка сделала с геометрией."""
        session = FakeSession()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "in" / "page.png"
            source.parent.mkdir()
            source.write_bytes(b"\x89PNG\r\n\x1a\n")

            out = root / "out"
            (out / "page.pages").mkdir(parents=True)
            (out / "page.omr.txt").write_text("отчёт стадий")
            (out / "page.clean.png").write_bytes(b"\x89PNG prepared frame")
            (out / "page.pages" / "page.p02.clean.png").write_bytes(b"\x89PNG page two")

            with mock.patch.object(failures, "SessionLocal", lambda: session), \
                 mock.patch.object(failures.settings, "failures_dir", str(root / "arch")):
                failures.record_failure(
                    task_id="t4", kind="single", preset=None, enhance=False,
                    input_paths=[source], error="нотных станов не найдено",
                    output_dir=out,
                )

            row = session.added[0]
            self.assertTrue(row.prepared_path.endswith("page.clean.png"), row.prepared_path)
            self.assertTrue(Path(row.prepared_path).exists())
            frames = sorted(p.name for p in (root / "arch" / "t4" / "prepared").iterdir())
            self.assertEqual(frames, ["page.clean.png", "page.pages_page.p02.clean.png"])
            # Логи и кадры лежат раздельно, иначе в архиве каша.
            self.assertTrue((root / "arch" / "t4" / "logs" / "page.omr.txt").exists())

    def test_a_failure_without_logs_is_still_recorded(self) -> None:
        session = FakeSession()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "page.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\n")

            with mock.patch.object(failures, "SessionLocal", lambda: session), \
                 mock.patch.object(failures.settings, "failures_dir", tmp):
                failures.record_failure(
                    task_id="t3", kind="single", preset=None, enhance=False,
                    input_paths=[source], error="что угодно",
                    output_dir=Path(tmp) / "нет такого каталога",
                )

        self.assertIsNone(session.added[0].log_path)
        self.assertIsNone(session.added[0].prepared_path)

    def test_missing_error_text_is_recorded_as_unknown(self) -> None:
        session = FakeSession()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "page.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\n")

            with mock.patch.object(failures, "SessionLocal", lambda: session), \
                 mock.patch.object(failures.settings, "failures_dir", tmp):
                failures.record_failure(
                    task_id=None, kind="single", preset=None, enhance=False,
                    input_paths=[source], error=None,
                )

        self.assertEqual(session.added[0].reason, FailureReason.UNKNOWN.value)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
