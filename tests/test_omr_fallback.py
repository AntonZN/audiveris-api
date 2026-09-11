"""Тесты цепочки распознавания: кто за кем и кого больше не зовут.

Цепочка теперь из двух звеньев, оба на homr: пайплайн `omr/`, а за ним homr на
сыром файле. Audiveris из неё выведен — его зовут только пресеты ударных (см.
tests/test_drums.py). Здесь сторожится и то, и другое: провал omr не проваливает
задачу, пока есть второе звено, — и с обычным пресетом Audiveris не зовётся даже
когда звенья кончились.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from api import omr_bridge, services
from api.exceptions import ProcessingError
from api.models import FileResult


class OmrFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.photo = self.out / "page.png"
        # Содержимое неважно: и omr, и homr тут замоканы, файл нужен только как путь.
        self.photo.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.addCleanup(self._tmp.cleanup)
        self.service = services.AudiverisService()

    def test_failed_omr_hands_the_file_to_the_previous_path(self) -> None:
        """Провал omr → зовём прежний путь, а не отдаём клиенту ошибку."""
        handed = FileResult(filename="page.mxl", url="/media/page.mxl")

        with mock.patch.object(omr_bridge, "run", side_effect=ProcessingError("омр не смог")), \
             mock.patch.object(services.homr_service, "run",
                               return_value=(self.out / "page.mxl", None)) as homr, \
             mock.patch.object(self.service, "_build_success_result", return_value=handed):
            result = self.service.process_single(self.photo, self.out)

        homr.assert_called_once()
        self.assertIsNone(result.error)
        self.assertEqual(result.filename, "page.mxl")

    def test_unexpected_crash_in_omr_is_also_survivable(self) -> None:
        """Не только ProcessingError: любое падение нового пути ловится."""
        with mock.patch.object(omr_bridge, "run", side_effect=RuntimeError("cv2 упал")), \
             mock.patch.object(services.homr_service, "run",
                               return_value=(self.out / "page.mxl", None)) as homr, \
             mock.patch.object(self.service, "_build_success_result",
                               return_value=FileResult(filename="page.mxl")):
            result = self.service.process_single(self.photo, self.out)

        homr.assert_called_once()
        self.assertIsNone(result.error)

    def test_successful_omr_never_touches_the_previous_path(self) -> None:
        """Обратная сторона: страховка не должна дублировать работу на успехе."""
        with mock.patch.object(omr_bridge, "run",
                               return_value=(self.out / "page.musicxml", None)), \
             mock.patch.object(services.homr_service, "run") as homr, \
             mock.patch.object(self.service, "_build_success_result",
                               return_value=FileResult(filename="page.musicxml")):
            result = self.service.process_single(self.photo, self.out)

        homr.assert_not_called()
        self.assertEqual(result.filename, "page.musicxml")

    def test_keeps_the_informative_reason_when_both_engines_fail(self) -> None:
        """omr объясняет провал отчётом стадий, homr на сыром файле — фразой
        «не смог распознать фото». Наверх должна уходить первая: именно она
        попадает в архив провалов и в метрику причин."""
        with mock.patch.object(
            omr_bridge, "run",
            side_effect=ProcessingError("стан слишком крупный (186.5px на интервал)"),
        ), mock.patch.object(
            services.homr_service, "run",
            side_effect=ProcessingError("homr не смог распознать фото"),
        ):
            result = self.service.process_single(self.photo, self.out)

        self.assertEqual(result.error, "стан слишком крупный (186.5px на интервал)")

    def test_the_second_attempt_reason_stands_on_its_own(self) -> None:
        """Если omr за файл не брался, отдаём то, что сказал homr."""
        odd = self.out / "page.bmp"
        odd.write_bytes(b"BM")

        with mock.patch.object(omr_bridge, "is_supported", return_value=False), \
             mock.patch.object(services, "is_photo", return_value=True), \
             mock.patch.object(services.homr_service, "run",
                               side_effect=ProcessingError("homr не смог распознать фото")):
            result = self.service.process_single(odd, self.out)

        self.assertEqual(result.error, "homr не смог распознать фото")

    def test_pdf_that_omr_failed_is_not_handed_to_audiveris(self) -> None:
        """PDF homr на сыром файле не читает, второго звена нет — задача падает с
        причиной от omr. Раньше здесь стоял Audiveris; проверяем, что не стоит."""
        pdf = self.out / "score.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")

        with mock.patch.object(omr_bridge, "run",
                               side_effect=ProcessingError("страницы без нот")), \
             mock.patch.object(self.service, "_run_audiveris") as audiveris, \
             mock.patch.object(services.homr_service, "run") as homr:
            result = self.service.process_single(pdf, self.out)

        audiveris.assert_not_called()
        homr.assert_not_called()
        self.assertEqual(result.error, "страницы без нот")

    def test_playlist_with_a_pdf_stays_on_the_omr_path(self) -> None:
        """PDF в плейлисте больше не переключает задачу на Audiveris compound book."""
        pdf = self.out / "score.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        merged = self.out / "playlist.musicxml"

        with mock.patch.object(self.service, "_run_homr_playlist",
                               return_value=(merged, None)) as playlist, \
             mock.patch.object(self.service, "_run_audiveris_playlist") as audiveris, \
             mock.patch.object(self.service, "_build_success_result",
                               return_value=FileResult(filename="playlist.musicxml")):
            result = self.service.process_playlist([self.photo, pdf], self.out)

        playlist.assert_called_once()
        audiveris.assert_not_called()
        self.assertIsNone(result.error)

    def test_says_plainly_when_no_engine_takes_the_file(self) -> None:
        odd = self.out / "score.sib"      # формат Sibelius: не растр и не PDF
        odd.write_bytes(b"\x00\x01\x02")

        with mock.patch.object(self.service, "_run_audiveris") as audiveris:
            result = self.service.process_single(odd, self.out)

        audiveris.assert_not_called()
        self.assertIn("не берёт ни один движок", result.error or "")

    def test_leftover_omr_log_cannot_pose_as_the_audiveris_log(self) -> None:
        """`_find_audiveris_log` берёт любой *.log в каталоге — забытый лог omr
        подменил бы собой лог Audiveris, и разбирать провал было бы нечем."""
        log = self.out / "page.omr.log"
        log.write_text("отчёт стадий")
        (self.out / "page.musicxml").write_text("<score/>")
        (self.out / "page.pages").mkdir()

        self.service._set_aside_omr_artefacts(self.photo, self.out)

        self.assertFalse(log.exists())
        self.assertTrue((self.out / "page.omr.txt").exists(), "лог не должен пропадать")
        self.assertFalse((self.out / "page.musicxml").exists())
        self.assertFalse((self.out / "page.pages").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
