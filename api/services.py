import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageEnhance

from api.config import settings
from api.exceptions import LowInterlineError, ProcessingError
from api.homr_service import homr_service, is_photo
from api.models import FileResult
from api.presets import Preset, get_preset_args

logger = logging.getLogger(__name__)


class AudiverisService:
    def _log_command_result(
        self,
        cmd: list[str],
        output_dir: Path,
        result: subprocess.CompletedProcess,
        timeout_seconds: int,
    ) -> None:
        if not settings.debug:
            if result.returncode != 0:
                logger.warning(
                    "Audiveris failed cmd=%s returncode=%s timeout=%ss output_dir=%s",
                    " ".join(cmd),
                    result.returncode,
                    timeout_seconds,
                    output_dir,
                )
            return

        logger.info(
            "Audiveris finished cmd=%s returncode=%s timeout=%ss output_dir=%s\nstdout:\n%s\nstderr:\n%s",
            " ".join(cmd),
            result.returncode,
            timeout_seconds,
            output_dir,
            result.stdout or "",
            result.stderr or "",
        )

    def _timeout_for_single(self) -> int:
        return max(settings.processing_timeout_per_file_seconds, 1)

    def _timeout_for_playlist(self, input_paths: list[Path]) -> int:
        return max(len(input_paths), 1) * self._timeout_for_single()

    def _remaining_timeout(self, started_at: float, total_timeout: int) -> int:
        elapsed = time.monotonic() - started_at
        return max(int(total_timeout - elapsed), 1)

    def _run_command(
        self,
        cmd: list[str],
        output_dir: Path,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess:
        process: subprocess.Popen | None = None
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            return subprocess.CompletedProcess(
                cmd,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
            else:
                stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or "")
                stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or "")
            result = subprocess.CompletedProcess(
                cmd,
                returncode=-1,
                stdout=stdout,
                stderr=stderr,
            )
            self._log_command_result(cmd, output_dir, result, timeout_seconds)
            log_path = self._write_log(output_dir, cmd, result)
            detail = f"Audiveris timed out after {timeout_seconds} seconds"
            raise ProcessingError(detail, log_path=log_path) from exc

    def _convert_webp_to_jpg(self, input_path: Path) -> Path:
        """Convert WebP image to JPG (Audiveris doesn't support WebP)."""
        if input_path.suffix.lower() != ".webp":
            return input_path

        jpg_path = input_path.with_suffix(".jpg")
        try:
            with Image.open(input_path) as img:
                # Convert to RGB if necessary (WebP may have alpha channel)
                if img.mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGB")
                img.save(jpg_path, "JPEG", quality=95)
            # Remove original WebP file
            input_path.unlink()
            return jpg_path
        except Exception:
            return input_path  # If conversion fails, try with original

    def _preprocess_image(self, input_path: Path) -> Path:
        """Preprocess image: convert WebP, upscale if small, enhance contrast and sharpness."""
        if input_path.suffix.lower() == ".pdf":
            return input_path  # Skip PDF files

        # Convert WebP to JPG first
        input_path = self._convert_webp_to_jpg(input_path)

        try:
            with Image.open(input_path) as img:
                needs_upscale = (
                    img.width < settings.image_min_dimension
                    or img.height < settings.image_min_dimension
                )

                if needs_upscale:
                    factor = settings.image_upscale_factor
                    new_size = (int(img.width * factor), int(img.height * factor))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                else:
                    return input_path

                # Enhance contrast
                if settings.image_contrast_factor != 1.0:
                    enhancer = ImageEnhance.Contrast(img)
                    img = enhancer.enhance(settings.image_contrast_factor)

                # Enhance sharpness
                if settings.image_sharpness_factor != 1.0:
                    enhancer = ImageEnhance.Sharpness(img)
                    img = enhancer.enhance(settings.image_sharpness_factor)

                # Save back if any changes were made
                if needs_upscale or settings.image_contrast_factor != 1.0 or settings.image_sharpness_factor != 1.0:
                    img.save(input_path)

        except Exception:
            pass  # If preprocessing fails, continue with original image

        return input_path

    def _prepare_input(self, input_path: Path, enhance: bool) -> Path:
        """Prepare an input image for Audiveris.

        With ``enhance`` on, run the aggressive phone/screenshot recipe (autocrop +
        upscale + adaptive threshold); otherwise the standard light preprocessing.
        """
        if enhance:
            from api.image_prep import enhance_for_omr

            return enhance_for_omr(self._convert_webp_to_jpg(input_path))
        return self._preprocess_image(input_path)

    def _build_success_result(
        self,
        output_path: Path,
        log_path: Path | None,
    ) -> FileResult:
        """Build a FileResult from a produced output file.

        Политика «fix-on-failure» (фикс — только когда без него файл непригоден):
          1) `collect_bpm` + `collect_texts` — выдёргиваем темп и распознанный
             текст из сырого .mxl ДО стрипа (мобиле они нужны как метаданные,
             а в xml-плеере всё равно не рисуются);
          2) `_strip_text_xml` — выкидываем текст и утечки путей плюс чиним
             структурные дефекты XML, на которых спотыкаются music21/verovio:
             <divisions>0</divisions> и клефы с <line> вне [1..5] (это и есть
             «line number must be 1-5» / «Could not find clef C-1»). .mxl на месте;
          3) `analyze_only` — music21 ТОЛЬКО парсит и считает analysis
             (тональность/размеры/темпы/инструменты);
          4) `verovio_check.midi_ok` — сначала проверяем прямо выход Audiveris
             (быстрый путь: на здоровых файлах тяжёлый round-trip не нужен). Если
             он НЕ собирается в MIDI, пробуем `repair` (music21 round-trip: parse →
             strip → rewrite, убирает beam-on-chord и пр.) и проверяем ещё раз.
             Получилось — отдаём починенный файл (`fixed=True`); не помогло —
             ProcessingError.
        """
        from api.analysis import _strip_text_xml, analyze_only, collect_bpm, collect_texts
        from api.models import ScoreTexts

        bpm: int | None = None
        try:
            bpm = collect_bpm(output_path)
        except Exception:
            logger.exception("bpm collection failed for %s", output_path)

        texts: ScoreTexts | None = None
        try:
            texts = ScoreTexts.model_validate(collect_texts(output_path))
        except Exception:
            logger.exception("text collection failed for %s", output_path)

        try:
            _strip_text_xml(output_path)
        except Exception:
            logger.exception("xml strip failed for %s", output_path)

        analysis = None
        try:
            analysis = analyze_only(output_path)
        except Exception:
            logger.exception("music21 analysis failed for %s", output_path)

        # verovio renderToMIDI — это «контракт» с мобильным клиентом: ровно тот же
        # вызов, что у него внутри. Проверяем СРАЗУ то, что отдал Audiveris —
        # на здоровых файлах music21-round-trip не нужен, не тратим его.
        from api.verovio_check import midi_ok

        result_path = output_path
        fixed = False

        if not midi_ok(result_path):
            # Выход не собирается в MIDI (частые причины: <beam> на ноте-члене
            # аккорда → segfault verovio, разбалансированные лиги, остаточные
            # клеф/divisions-дефекты). Пробуем ПОЧИНИТЬ прогоном через music21
            # (repair: parse → strip → rewrite, см. analysis.repair) и проверяем
            # ещё раз — fallback, тяжёлый round-trip только когда без него никак.
            from api.analysis import repair

            fixed_path = None
            try:
                fixed_path = repair(output_path)
            except Exception:
                logger.exception("music21 repair failed for %s", output_path)

            if fixed_path is not None and midi_ok(fixed_path):
                result_path = fixed_path
                fixed = True
            else:
                detail = (
                    "Не удалось получить валидный MusicXML: выход Audiveris не "
                    "рендерится в MIDI через verovio даже после music21-фикса"
                )
                raise ProcessingError(detail, log_path=log_path)

        return FileResult(
            filename=result_path.name,
            url=self._build_media_url(result_path),
            log_url=self._build_media_url(log_path) if log_path else None,
            fixed=fixed,
            bpm=bpm,
            analysis=analysis,
            texts=texts,
        )

    def process_single(
        self,
        input_path: Path,
        output_dir: Path,
        preset: str = "default",
        enhance: bool = False,
    ) -> FileResult:
        """Process a single input file and return a FileResult.

        Любое одиночное ИЗОБРАЖЕНИЕ (JPEG/HEIC/PNG/WebP) распознаём через homr —
        он устойчивее к перекосу/шуму/перспективе. Только PDF идёт стандартным
        путём через Audiveris. Постобработка (analysis/bpm/texts + проверка
        midi_ok) для обоих движков одна и та же.
        """
        try:
            if settings.homr_enabled and is_photo(input_path):
                output_path, log_path = homr_service.run(input_path, output_dir)
                result = self._build_success_result(output_path, log_path)
                # homr слабо распознаёт темп: если BPM не нашёлся — добираем его
                # через Audiveris (он надёжнее) и вписываем и в файл, и в API-поле.
                if result.bpm is None and settings.audiveris_bpm_fallback:
                    bpm = self._extract_bpm_via_audiveris(input_path, output_dir)
                    if bpm is not None:
                        from api.analysis import inject_bpm

                        inject_bpm(output_path, bpm)  # в отдаваемый файл (best-effort)
                        result.bpm = bpm              # в ответ API — в любом случае
                return result

            output_path, log_path, interline = self._run_audiveris(
                input_path, output_dir, preset, enhance
            )
            return self._build_success_result(output_path, log_path)
        except LowInterlineError as exc:
            return FileResult(
                filename=input_path.name,
                error=exc.message,
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )
        except ProcessingError as exc:
            return FileResult(
                filename=input_path.name,
                error=exc.message,
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )

    def _extract_bpm_via_audiveris(self, input_path: Path, output_dir: Path) -> int | None:
        """Best-effort: достать BPM фото через Audiveris, когда homr темп не нашёл.

        Гоняем Audiveris на ИСХОДНОМ снимке во временный подкаталог и читаем темп
        из его MusicXML. Намеренно НЕ препроцессим (upscale размывает мелкие цифры
        темпа и ломает их OCR — см. image_min_dimension в config). Любая ошибка
        или таймаут → None: проба не должна валить уже успешную задачу.
        """
        from api.analysis import collect_bpm

        probe_dir = output_dir / ".bpm_probe"
        try:
            shutil.rmtree(probe_dir, ignore_errors=True)
            probe_dir.mkdir(parents=True, exist_ok=True)

            # Копируем вход, чтобы не трогать оригинал; WebP -> JPG (Audiveris его
            # не читает). _preprocess_image сознательно не вызываем.
            probe_input = probe_dir / input_path.name
            shutil.copyfile(input_path, probe_input)
            probe_input = self._convert_webp_to_jpg(probe_input)

            cmd = [
                settings.audiveris_cmd,
                "-batch",
                "-constant",
                f"org.audiveris.omr.sheet.ScaleBuilder.minInterline={settings.min_interline}",
                *self._NO_MOVEMENT_SPLIT,
                "-transcribe",
                "-export",
                "-output", str(probe_dir),
                str(probe_input),
            ]
            result = self._run_command(cmd, probe_dir, self._timeout_for_single())
            if result.returncode != 0:
                return None
            candidates = self._find_outputs(probe_dir)
            if not candidates:
                return None
            return collect_bpm(sorted(candidates)[0])
        except ProcessingError:
            return None  # таймаут Audiveris (_run_command)
        except Exception:
            logger.exception("audiveris bpm probe failed for %s", input_path)
            return None
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)

    def process_playlist(
        self,
        input_paths: list[Path],
        output_dir: Path,
        preset: str = "default",
        enhance: bool = False,
    ) -> FileResult:
        """Process multiple files as a playlist (one song across pages).

        Если все входы — ФОТО и включён homr: каждое фото распознаём homr'ом
        отдельно (он устойчивее к перекосу/шуму/перспективе), затем склеиваем
        постранично через relieur (см. _run_homr_playlist). Иначе (PDF в
        плейлисте или homr выключен) — прежний путь через Audiveris compound book.
        Постобработка (analysis/bpm/texts + midi_ok + repair) для обоих одна.
        """
        try:
            if (
                settings.homr_enabled
                and input_paths
                and all(is_photo(p) for p in input_paths)
            ):
                output_path, log_path = self._run_homr_playlist(input_paths, output_dir)
                result = self._build_success_result(output_path, log_path)
                # homr слабо распознаёт темп — добираем его Audiveris'ом по первой
                # странице (как в single) и вписываем и в файл, и в ответ API.
                if result.bpm is None and settings.audiveris_bpm_fallback:
                    bpm = self._extract_bpm_via_audiveris(input_paths[0], output_dir)
                    if bpm is not None:
                        from api.analysis import inject_bpm

                        inject_bpm(output_path, bpm)
                        result.bpm = bpm
                return result

            output_path, log_path, interline = self._run_audiveris_playlist(
                input_paths, output_dir, preset, enhance
            )
            return self._build_success_result(output_path, log_path)
        except LowInterlineError as exc:
            return FileResult(
                filename="playlist",
                error=exc.message,
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )
        except ProcessingError as exc:
            return FileResult(
                filename="playlist",
                error=exc.message,
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )

    def _run_homr_playlist(
        self, input_paths: list[Path], output_dir: Path
    ) -> tuple[Path, Path | None]:
        """Распознать каждое фото плейлиста через homr и склеить в одну партитуру.

        Каждое фото → homr → .mxl (тот же путь, что и single), в свой подкаталог
        .page{i} ради раздельных логов и чтобы .mxl не перетирали друг друга.
        Затем relieur.merge_musicxml дописывает такты каждой следующей страницы в
        хвост соответствующей партии первой, сквозным образом перенумеровывает
        такты и снимает повторную декларацию key/clef/divisions на стыке страниц.

        Сбой homr на ЛЮБОЙ странице (ProcessingError из homr_service.run) и любая
        ошибка склейки роняют всю задачу — отдавать песню с дырой посередине
        бессмысленно.

        Returns:
            (merged_path, log_path) — склеенный .musicxml и лог последней страницы.
        """
        from api.relieur import merge_musicxml

        page_mxls: list[Path] = []
        last_log: Path | None = None
        for i, input_path in enumerate(input_paths):
            mxl_path, log_path = homr_service.run(input_path, output_dir / f".page{i}")
            page_mxls.append(mxl_path)
            last_log = log_path

        merged_path = output_dir / "playlist.musicxml"
        try:
            merge_musicxml(page_mxls, merged_path)
        except Exception as exc:
            logger.exception("relieur merge failed for playlist in %s", output_dir)
            raise ProcessingError(
                f"Не удалось склеить страницы плейлиста: {exc}",
                log_path=last_log,
            ) from exc
        return merged_path, last_log

    # Audiveris ловит «новый movement» по indented system'у (см. SystemManager.java).
    # На фотках/скриншотах одной песни это даёт false positive: если на каком-то
    # system OMR не нашёл один из staff'ов, его левый край «сдвигается» и Audiveris
    # начинает писать .mvt2.mxl, .mvt3.mxl. На выходе клиенту уходит только .mvt1
    # (sorted()[0] в _find_outputs) — мобила играет первую треть песни и обрывает.
    # Для одиночной фотки и плейлиста (всегда одна песня по нескольким снимкам)
    # детекцию выключаем; для многостраничного PDF оставляем — он реально может
    # быть многочастной сонатой, где разделение полезно.
    _NO_MOVEMENT_SPLIT = [
        "-constant", "org.audiveris.omr.sheet.ProcessingSwitches.indentations=false",
    ]

    def _run_audiveris(
            self, input_path: Path, output_dir: Path, preset: str = "default",
            enhance: bool = False,
    ) -> tuple[Path, Path, int | None]:
        """Run audiveris on a single input file."""
        input_path = self._prepare_input(input_path, enhance)

        # Build command with preset
        preset_enum = Preset(preset) if preset else Preset.default
        preset_args = get_preset_args(preset_enum)

        is_pdf = input_path.suffix.lower() == ".pdf"
        movement_args = [] if is_pdf else self._NO_MOVEMENT_SPLIT

        cmd = [
            settings.audiveris_cmd,
            "-batch",
            "-constant", f"org.audiveris.omr.sheet.ScaleBuilder.minInterline={settings.min_interline}",
            # *movement_args,
            *preset_args,
            "-transcribe", "-export",
            "-output", str(output_dir),
            str(input_path),
        ]
        return self._execute_and_process(
            cmd,
            output_dir,
            timeout_seconds=self._timeout_for_single(),
        )

    def _execute_and_process(
            self, cmd: list[str], output_dir: Path, timeout_seconds: int
    ) -> tuple[Path, Path, int | None]:
        """Execute audiveris command and process results."""
        result = self._run_command(cmd, output_dir, timeout_seconds)
        self._log_command_result(cmd, output_dir, result, timeout_seconds)
        log_path = self._write_log(output_dir, cmd, result)
        book_log = self._find_audiveris_log(output_dir, log_path)
        interline_value = self._detect_interline(book_log)

        if interline_value is not None and interline_value < settings.min_interline:
            detail = (
                f"Image resolution too low: interline={interline_value}px < {settings.min_interline}px"
            )
            raise LowInterlineError(interline_value, detail, book_log)

        if result.returncode != 0:
            error = (result.stderr or result.stdout or "Audiveris failed").strip()
            detail = f"Audiveris failed. {error}"
            raise ProcessingError(detail, log_path=book_log)

        # Check for errors in stdout (Audiveris may return 0 even with errors)
        processing_errors = self._detect_processing_errors(result.stdout or "")

        candidates = self._find_outputs(output_dir)
        if not candidates:
            files = self._list_files(output_dir)
            error_info = f" Errors: {processing_errors}" if processing_errors else ""
            detail = f"No MusicXML output found, files={files}).{error_info}"
            raise ProcessingError(detail, log_path=book_log)

        output_path = sorted(candidates)[0]
        return output_path, book_log, interline_value

    def _create_playlist_xml(self, input_paths: list[Path], output_dir: Path) -> Path:
        """Create a playlist XML file for audiveris."""
        playlist_path = output_dir / "playlist.xml"
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<play-list>"]
        for path in input_paths:
            lines.append(f"  <excerpt>")
            lines.append(f"    <path>{path}</path>")
            lines.append(f"  </excerpt>")
        lines.append("</play-list>")
        playlist_path.write_text("\n".join(lines))
        return playlist_path

    def _run_audiveris_playlist(
            self, input_paths: list[Path], output_dir: Path, preset: str = "default",
            enhance: bool = False,
    ) -> tuple[Path, Path, int | None]:
        """Run audiveris with playlist.

        Step 1: Create compound book from playlist (images -> playlist.omr)
        Step 2: Transcribe and export the compound book
        """
        all_logs: list[str] = []
        total_timeout = self._timeout_for_playlist(input_paths)
        started_at = time.monotonic()

        # Build preset args
        preset_enum = Preset(preset) if preset else Preset.default
        preset_args = get_preset_args(preset_enum)

        # Preprocess all input images (may convert WebP to JPG)
        processed_paths = [self._prepare_input(p, enhance) for p in input_paths]

        # Step 1: Create compound book from playlist
        playlist_path = self._create_playlist_xml(processed_paths, output_dir)
        cmd_build = [
            settings.audiveris_cmd,
            "-batch",
            "-constant", f"org.audiveris.omr.sheet.ScaleBuilder.minInterline={settings.min_interline}",
            # Плейлист — это всегда одна песня по нескольким фотографиям, поэтому
            # детекцию indented systems выключаем (см. _NO_MOVEMENT_SPLIT). Аргумент
            # дублируется и в cmd_export, потому что -constant действует только в
            # рамках одного JVM-вызова Audiveris.
            *self._NO_MOVEMENT_SPLIT,
            *preset_args,
            "-playlist", str(playlist_path),
            "-output", str(output_dir),
        ]
        result_build = self._run_command(
            cmd_build,
            output_dir,
            self._remaining_timeout(started_at, total_timeout),
        )
        all_logs.append(f"=== Step 1: Build compound book ===")
        all_logs.append(f"cmd: {' '.join(cmd_build)}")
        all_logs.append(result_build.stdout or "")
        if result_build.stderr:
            all_logs.append(result_build.stderr)

        # Find compound .omr file
        compound_omr = output_dir / "playlist.omr"
        if not compound_omr.exists():
            log_path = output_dir / "audiveris.log"
            log_path.write_text("\n".join(all_logs))
            raise ProcessingError(
                f"Compound book not created",
                log_path=log_path
            )

        # Step 2: Transcribe and export compound book
        cmd_export = [
            settings.audiveris_cmd,
            "-batch",
            "-constant", f"org.audiveris.omr.sheet.ScaleBuilder.minInterline={settings.min_interline}",
            *self._NO_MOVEMENT_SPLIT,
            *preset_args,
            "-transcribe",
            "-export",
            "-output", str(output_dir),
            str(compound_omr),
        ]
        all_logs.append(f"\n=== Step 2: Transcribe and export ===")
        all_logs.append(f"cmd: {' '.join(cmd_export)}")

        # Write intermediate log
        log_path = output_dir / "audiveris.log"
        log_path.write_text("\n".join(all_logs))

        return self._execute_and_process(
            cmd_export,
            output_dir,
            timeout_seconds=self._remaining_timeout(started_at, total_timeout),
        )

    def _write_log(
            self, out_dir: Path, cmd: list[str], result: subprocess.CompletedProcess
    ) -> Path:
        """Write audiveris execution log."""
        log_path = out_dir / "audiveris.log"
        payload = [
            f"cmd: {' '.join(cmd)}",
            f"returncode: {result.returncode}",
            "",
            "stdout:",
            result.stdout or "",
            "",
            "stderr:",
            result.stderr or "",
        ]
        log_path.write_text("\n".join(payload))
        return log_path

    def _find_audiveris_log(self, out_dir: Path, fallback: Path) -> Path:
        """Find the audiveris-generated log file."""
        logs = [path for path in out_dir.rglob("*.log") if path.is_file()]
        logs = [path for path in logs if path.name != fallback.name]
        if not logs:
            return fallback
        logs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return logs[0]

    def _detect_interline(self, log_path: Path) -> int | None:
        """Detect interline value from log file."""
        if not log_path.exists():
            return None
        content = log_path.read_text(errors="ignore")
        values = [
            int(match.group(1))
            for match in re.finditer(r"interline value of (\d+) pixels", content)
        ]
        if not values:
            return None
        return min(values)

    def _detect_processing_errors(self, stdout: str) -> list[str]:
        """Detect processing errors from Audiveris stdout."""
        errors = []
        for line in stdout.split("\n"):
            # Look for WARN/ERROR lines with exceptions
            if "Error in performing" in line or "Exception" in line:
                errors.append(line.strip())
            # Look for specific error patterns
            elif "WARN" in line and ("Error" in line or "null" in line.lower()):
                errors.append(line.strip())
        return errors[:5]  # Limit to first 5 errors

    def _find_outputs(self, out_dir: Path) -> list[Path]:
        """Find all MusicXML output files (.mxl preferred)."""
        # Exclude known non-output files
        exclude_names = {"playlist.xml"}

        mxl_files = []
        xml_files = []

        for path in out_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name.lower() in exclude_names:
                continue
            suffix = path.suffix.lower()
            if suffix == ".mxl":
                mxl_files.append(path)
            elif suffix == ".xml":
                # Only include XML files that look like MusicXML (not internal Audiveris files)
                # Audiveris stores internal data in subdirectories like /sheet#1/sheet#1.xml
                if path.parent == out_dir:
                    xml_files.append(path)

        # Prefer .mxl files over .xml
        return mxl_files if mxl_files else xml_files

    def _list_files(self, root: Path) -> str:
        """List files in directory for error messages."""
        if not root.exists():
            return "none"
        files = sorted(
            str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
        )
        if not files:
            return "none"
        if len(files) > settings.max_listed_files:
            return (
                    ", ".join(files[: settings.max_listed_files])
                    + f", ... (+{len(files) - settings.max_listed_files} more)"
            )
        return ", ".join(files)

    def _build_media_url(self, path: Path) -> str | None:
        """Build a media URL for a file path."""
        try:
            media_root = Path(settings.media_root)
            rel = path.relative_to(media_root)
        except ValueError:
            return None

        rel_posix = quote(rel.as_posix())
        base = settings.media_base_url.rstrip("/")
        prefix = settings.media_path_prefix.strip("/")

        if prefix:
            return f"{base}/{prefix}/{rel_posix}"
        return f"{base}/{rel_posix}"


audiveris_service = AudiverisService()
