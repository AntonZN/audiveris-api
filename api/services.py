import logging
import os
import re
import shutil
import signal
import statistics
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageEnhance

from api.config import settings
from api.exceptions import LowInterlineError, ProcessingError
from api import drums, omr_bridge
from api.homr_service import homr_service, is_photo
from api.models import FileResult, ScoreTexts
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

    def _pdf_render_dpi(self, pdf_path: Path) -> int | None:
        """Безопасный DPI рендера PDF, чтобы самая большая страница уложилась в
        pdf_max_pixels (иначе Audiveris отбросит лист как «Too large image» и не
        создаст ни одной партитуры). None → PDF влезает на полном pdf_render_dpi
        (или не удалось прочитать) — оставляем дефолт Audiveris.

        Пиксели листа при DPI d = (w_pt·d/72)·(h_pt·d/72) = w_pt·h_pt·(d/72)².
        Отсюда «влезающий» DPI = 72·sqrt(budget / (w_pt·h_pt)).
        """
        import math

        from pypdf import PdfReader

        try:
            reader = PdfReader(str(pdf_path))
            max_pts2 = 0.0
            for page in reader.pages:
                box = page.mediabox
                max_pts2 = max(max_pts2, float(box.width) * float(box.height))
            if max_pts2 <= 0:
                return None
            fitting = 72.0 * math.sqrt(settings.pdf_max_pixels / max_pts2)
            if fitting >= settings.pdf_render_dpi:
                return None  # влезает на полном DPI — ничего не навязываем
            return max(1, int(fitting))
        except Exception:
            logger.exception("failed to compute safe pdf dpi for %s", pdf_path)
            return None

    def _pdf_resolution_args(self, paths: list[Path]) -> list[str]:
        """`-constant`-аргументы, ограничивающие DPI рендера PDF среди входов.

        Берём минимальный безопасный DPI по всем PDF-входам (картинки игнорируем —
        их Audiveris рендерит как есть). Пусто, если ни одному PDF ограничение не
        нужно."""
        dpis = [
            d
            for p in paths
            if p.suffix.lower() == ".pdf"
            for d in (self._pdf_render_dpi(p),)
            if d is not None
        ]
        if not dpis:
            return []
        return [
            "-constant",
            f"org.audiveris.omr.image.ImageLoading.pdfResolution={min(dpis)}",
        ]

    def _prepare_input(self, input_path: Path, enhance: bool) -> Path:
        """Prepare an input image for Audiveris.

        With ``enhance`` on, run the aggressive phone/screenshot recipe (autocrop +
        upscale + adaptive threshold); otherwise the standard light preprocessing.
        """
        if enhance:
            from api.image_prep import enhance_for_omr

            return enhance_for_omr(self._convert_webp_to_jpg(input_path))
        return self._preprocess_image(input_path)

    @staticmethod
    def _tempo_candidates(texts: ScoreTexts) -> list[str]:
        """Тексты, где может стоять метрономная отметка, в порядке правдоподобия.

        Заголовок первый: у homr фильтр `is_tempo_marking` пропускает в заголовок
        всё, в чём есть четыре буквы, поэтому «Allegro ♩=120» приезжает целиком.
        Слоги и имена партий не смотрим — там темпа не бывает, а числа бывают.
        """
        candidates = [texts.title or "", texts.composer or ""]
        candidates += list(texts.credits or [])
        candidates += [d.text for d in (texts.directions or []) if d.text]
        return [text for text in candidates if text]

    def _build_success_result(
        self,
        output_path: Path,
        log_path: Path | None,
    ) -> FileResult:
        """Build a FileResult from a produced output file.

        Политика «fix-on-failure» (фикс — только когда без него файл непригоден):
          1) `collect_bpm` + `collect_texts` — выдёргиваем темп и распознанный
             текст из сырого .mxl ДО стрипа (мобиле они нужны как метаданные,
             а в xml-плеере всё равно не рисуются). Темпа в файле нет — ищем
             метрономную отметку в самом тексте (`bpm_from_texts`): homr её из
             картинки не пишет никогда, а отдельного движка ради одного числа
             мы больше не гоняем;
          2) `_strip_text_xml` — выкидываем текст и утечки путей плюс чиним
             структурные дефекты XML, на которых спотыкаются music21/verovio:
             <divisions>0</divisions> и клефы с <line> вне [1..5] (это и есть
             «line number must be 1-5» / «Could not find clef C-1»). .mxl на месте;
          3) `analyze_only` — music21 ТОЛЬКО парсит и считает analysis
             (тональность/размеры/темпы/инструменты);
          4) `verovio_check.renders_ok` — сначала проверяем прямо выход движка
             (быстрый путь: на здоровых файлах тяжёлый round-trip не нужен). Если
             verovio его не принимает, пробуем `repair` (music21 round-trip: parse →
             strip → rewrite, убирает beam-on-chord и пр.) и проверяем ещё раз.
             Получилось — отдаём починенный файл (`fixed=True`);
          5) `salvage` — если не помог и `repair`, вместо провала задачи
             локализуем проблемное место бисекцией и выбрасываем его. Клиенту
             уходит НЕПОЛНАЯ партитура с `dropped_measures > 0` — это лучше, чем
             ничего. Не удалось и это — ProcessingError.
        """
        from api.analysis import _strip_text_xml, analyze_only, collect_bpm, collect_texts

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

        # Темпа в файле нет — поищем его в тексте, который уже распознан. Отметка
        # часто срослась с заголовком («Allegro ♩ = 120») или лежит в ремарке. Это
        # даром, а альтернатива — отдельный запуск Audiveris ради одного числа.
        if bpm is None and texts is not None:
            try:
                from api.analysis import bpm_from_texts, inject_bpm

                bpm = bpm_from_texts(self._tempo_candidates(texts))
                if bpm is not None:
                    inject_bpm(output_path, bpm)
            except Exception:
                logger.exception("bpm from texts failed for %s", output_path)

        try:
            _strip_text_xml(output_path)
        except Exception:
            logger.exception("xml strip failed for %s", output_path)

        analysis = None
        try:
            analysis = analyze_only(output_path)
        except Exception:
            logger.exception("music21 analysis failed for %s", output_path)

        # Гейт «пустого» результата: мусорный вход (скриншот, фото без нот) homr/
        # Audiveris «успешно» превращают в почти пустую партитуру. Если нот меньше
        # порога — это не успех, а провал распознавания: роняем задачу с ошибкой,
        # чтобы файл ушёл в архив провалов, а не отдался клиентом как completed с
        # пустым .mxl. Порог 0 отключает проверку. analysis=None (music21 не смог
        # распарсить) НЕ трогаем — там своё поведение, гейт только по факту 0..N нот.
        if settings.min_recognized_notes > 0 and analysis is not None:
            notes = int(analysis.get("notes") or 0)
            if notes < settings.min_recognized_notes:
                detail = (
                    f"Распознавание не нашло музыки: {notes} нот в выходе "
                    f"(порог {settings.min_recognized_notes}). Похоже, на входе "
                    "не партитура (скриншот/фото без нот) или качество слишком низкое."
                )
                raise ProcessingError(detail, log_path=log_path)

        # verovio — это «контракт» с мобильным клиентом: те же вызовы, что у него
        # внутри (загрузка + MIDI + вёрстка страницы). Проверяем СРАЗУ то, что
        # отдал движок — на здоровых файлах music21-round-trip не нужен.
        from api.verovio_check import renders_ok

        result_path = output_path
        fixed = False
        dropped_measures = 0

        if not renders_ok(result_path):
            # Выход не принимается verovio (частые причины: <beam> на ноте-члене
            # аккорда → segfault, разбалансированные лиги, остаточные
            # клеф/divisions-дефекты). Пробуем ПОЧИНИТЬ прогоном через music21
            # (repair: parse → strip → rewrite, см. analysis.repair) и проверяем
            # ещё раз — fallback, тяжёлый round-trip только когда без него никак.
            from api.analysis import repair

            fixed_path = None
            try:
                fixed_path = repair(output_path)
            except Exception:
                logger.exception("music21 repair failed for %s", output_path)

            if fixed_path is not None and renders_ok(fixed_path):
                result_path = fixed_path
                fixed = True
            else:
                # Последняя ступень перед провалом задачи: локализовать
                # проблемное место бисекцией и выбросить его (см. analysis.salvage).
                # Партитура без одного такта играбельна, а провал задачи — нет.
                # Спасаем из выхода music21, если он получился: там XML уже
                # нормализован, и резать его безопаснее.
                from api.analysis import salvage

                source = fixed_path if fixed_path is not None else output_path
                rescued = None
                try:
                    rescued = salvage(source, renders_ok, out_dir=output_path.parent)
                except Exception:
                    logger.exception("salvage failed for %s", source)

                if rescued is None:
                    detail = (
                        "Не удалось получить валидный MusicXML: verovio не принимает "
                        "выход движка ни после music21-фикса, ни после удаления "
                        "проблемных тактов"
                    )
                    raise ProcessingError(detail, log_path=log_path)

                result_path, report = rescued
                fixed = True
                dropped_measures = int(report["dropped_measures"])

        return FileResult(
            filename=result_path.name,
            url=self._build_media_url(result_path),
            log_url=self._build_media_url(log_path) if log_path else None,
            fixed=fixed,
            dropped_measures=dropped_measures,
            bpm=bpm,
            analysis=analysis,
            texts=texts,
        )

    @staticmethod
    def _can_recognise(path: Path) -> bool:
        """Возьмётся ли за файл хоть один движок цепочки (omr или homr)."""
        return (
            settings.omr_pipeline_enabled and omr_bridge.is_supported(path)
        ) or (settings.homr_enabled and is_photo(path))

    def process_single(
        self,
        input_path: Path,
        output_dir: Path,
        preset: str = "default",
        enhance: bool = False,
    ) -> FileResult:
        """Process a single input file and return a FileResult.

        Распознают два движка, оба на homr:
          1) пайплайн `omr/` (см. api/omr_bridge.py) — он готовит страницу и
             зовёт homr, а PDF растеризует постранично. Берёт и растр, и PDF;
          2) homr напрямую на сырой файл — второй и последний шанс для растра,
             если подготовка страницы сделала хуже. Включается и при
             `omr_pipeline_enabled=false` (откат без выката кода), но PDF он не
             читает, так что для PDF первый путь единственный.

        Audiveris из этой цепочки выведен: не смог homr — значит не смог. Раньше он
        стоял тут третьей попыткой и добирал темп отдельным прогоном.

        Исключение — пресеты ударных (`drums`, `drums_1line`): их homr не знает,
        поэтому они идут прямо в Audiveris, минуя обе попытки (`_recognize_drums`).

        Постобработка (analysis/bpm/texts + renders_ok + repair + salvage) для
        всех путей одна и та же.
        """
        try:
            if drums.is_drum_preset(preset):
                return self._recognize_drums(input_path, output_dir, preset, enhance)

            omr_failure: ProcessingError | None = None
            if settings.omr_pipeline_enabled and omr_bridge.is_supported(input_path):
                result, omr_failure = self._try_omr(input_path, output_dir)
                if result is not None:
                    return result

            if settings.homr_enabled and is_photo(input_path):
                try:
                    output_path, log_path = homr_service.run(input_path, output_dir)
                    return self._build_success_result(output_path, log_path)
                except ProcessingError as exc:
                    if omr_failure is None:
                        raise
                    # Не смогли оба. Наверх отдаём причину от omr: в ней отчёт
                    # стадий и ссылка на лог, а «homr не смог распознать фото»
                    # не говорит ни клиенту, ни нам ничего. Замерено на боевом
                    # файле: omr писал «стан слишком крупный (186.5px)», а в
                    # архив провалов уезжало пустое «движок не справился».
                    logger.warning(
                        "homr на сыром файле тоже не смог (%s) — отдаём причину omr",
                        exc.message,
                    )
                    raise omr_failure from exc

            if omr_failure is not None:
                raise omr_failure
            raise ProcessingError(
                f"Файл {input_path.name} не берёт ни один движок: "
                "omr выключен или не знает такой формат, а homr читает только растр"
            )
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

    def _try_omr(
        self, input_path: Path, output_dir: Path
    ) -> tuple[FileResult | None, ProcessingError | None]:
        """Пайплайн `omr/` для одного файла.

        Возвращает (результат, причина провала): ровно одно из двух не None.
        Причину возвращаем, а не бросаем, потому что за omr может стоять вторая
        попытка (homr на сыром файле) — но если её нет, именно эта причина уйдёт
        клиенту и в архив провалов, поэтому она несёт свой лог.
        """
        try:
            output_path, log_path = omr_bridge.run(input_path, output_dir)
            return self._build_success_result(output_path, log_path), None
        except ProcessingError as exc:
            logger.warning("omr не справился с %s: %s", input_path.name, exc.message)
            log_path = self._set_aside_omr_artefacts(input_path, output_dir)
            return None, ProcessingError(exc.message, log_path=log_path or exc.log_path)
        except Exception as exc:
            logger.exception("omr упал на %s", input_path.name)
            log_path = self._set_aside_omr_artefacts(input_path, output_dir)
            return None, ProcessingError(
                f"Пайплайн omr упал на {input_path.name}: {type(exc).__name__}: {exc}",
                log_path=log_path,
            )

    @staticmethod
    def _set_aside_omr_artefacts(input_path: Path, output_dir: Path) -> Path | None:
        """Убрать недоделанный выход omr, сохранив лог. Возвращает путь лога.

        Лог переименовываем из `*.log`: `_find_audiveris_log` берёт любой `*.log`
        в каталоге по времени, и сейчас, когда Audiveris из цепочки выведен, это
        не мешает — но помешает, когда его подключат в другом месте.
        """
        stem = input_path.stem
        log_path = output_dir / f"{stem}.omr.log"
        kept: Path | None = None
        if log_path.exists():
            kept = output_dir / f"{stem}.omr.txt"
            log_path.replace(kept)
        # `.clean.png` НЕ удаляем: это кадр, который ушёл в движок, и он уезжает
        # в архив провалов как главный экспонат для разбора. Прежнему пути он не
        # мешает — тот ищет свои выходы по расширению.
        (output_dir / f"{stem}.musicxml").unlink(missing_ok=True)
        shutil.rmtree(output_dir / f"{stem}.pages", ignore_errors=True)
        return kept

    def process_playlist(
        self,
        input_paths: list[Path],
        output_dir: Path,
        preset: str = "default",
        enhance: bool = False,
    ) -> FileResult:
        """Process multiple files as a playlist (one song across pages).

        Каждый вход распознаётся отдельно и постранично склеивается через relieur
        (см. _run_homr_playlist). PDF в плейлисте больше не переключает задачу на
        Audiveris compound book: пайплайн `omr/` растеризует его сам, страница за
        страницей, и склейка не знает, откуда взялась страница.

        Пресеты ударных — как и в single, прямо в Audiveris (compound book).

        Постобработка (analysis/bpm/texts + renders_ok + repair + salvage) та же,
        что и в single.
        """
        try:
            if drums.is_drum_preset(preset):
                return self._recognize_drums_playlist(input_paths, output_dir, preset, enhance)

            if input_paths and all(self._can_recognise(p) for p in input_paths):
                output_path, log_path = self._run_homr_playlist(input_paths, output_dir)
                return self._build_success_result(output_path, log_path)

            unknown = [p.name for p in input_paths if not self._can_recognise(p)]
            raise ProcessingError(
                "В плейлисте есть файлы, которые не берёт ни один движок: "
                + (", ".join(unknown) or "плейлист пуст")
            )
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

        Каждое фото → omr → .musicxml (тот же путь, что и single), в свой
        подкаталог .page{i} ради раздельных логов и чтобы выходы не перетирали
        друг друга.
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
            # Тот же путь, что и в single: подготовка страницы пакетом omr, затем
            # движок. Снимок разворота при этом сам разложится на две страницы и
            # склеится внутри omr — relieur получит один файл на входной снимок,
            # как и раньше.
            page_dir = output_dir / f".page{i}"
            mxl_path = log_path = None
            if settings.omr_pipeline_enabled and omr_bridge.is_supported(input_path):
                try:
                    mxl_path, log_path = omr_bridge.run(input_path, page_dir)
                except Exception:
                    # Одна страница не должна ронять плейлист из-за нового пути:
                    # отдаём её прежнему (homr на сырой снимок), как в single.
                    logger.exception(
                        "omr не справился со страницей %s — пробуем прежний путь",
                        input_path.name,
                    )
                    self._set_aside_omr_artefacts(input_path, page_dir)
                    mxl_path = None
            if mxl_path is None:
                mxl_path, log_path = homr_service.run(input_path, page_dir)
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

    # ------------------------------------------------------------------------
    # Audiveris. Из общей цепочки распознавания выведен (2026-09-09): темп теперь
    # читается из OCR, который homr и так делает, а третьей попыткой Audiveris
    # стоял дорого и редко помогал. Зовётся только для пресетов ударных — их homr
    # не знает (см. api/drums.py); `preset` и `enhance` действуют только здесь.
    # ------------------------------------------------------------------------

    def _recognize_drums(
            self, input_path: Path, output_dir: Path, preset: str, enhance: bool,
    ) -> FileResult:
        """Ударные: сразу Audiveris, без omr и homr.

        Провал — честная ошибка, без отката на homr: он читает перкуссионный ключ
        как альтовый и выдал бы «успешную» партитуру из неверных нот.
        """
        prepared = self._audiveris_readable(input_path, enhance)

        def run(target: Path, extra: list[str], timeout: int) -> tuple[Path, Path, int | None]:
            return self._run_audiveris(prepared, target, preset, extra, timeout)

        output_path, log_path, _ = self._drum_attempts(
            output_dir, [prepared], preset, run, self._timeout_for_single()
        )
        return self._build_success_result(output_path, log_path)

    def _recognize_drums_playlist(
            self, input_paths: list[Path], output_dir: Path, preset: str, enhance: bool,
    ) -> FileResult:
        """Плейлист ударных: одна compound-книга Audiveris из всех страниц."""
        if not input_paths:
            raise ProcessingError("Плейлист пуст")
        prepared = [self._audiveris_readable(p, enhance) for p in input_paths]

        def run(target: Path, extra: list[str], timeout: int) -> tuple[Path, Path, int | None]:
            return self._run_audiveris_playlist(prepared, target, preset, extra, timeout)

        output_path, log_path, _ = self._drum_attempts(
            output_dir, prepared, preset, run, self._timeout_for_playlist(prepared)
        )
        return self._build_success_result(output_path, log_path)

    def _drum_attempts(
            self, output_dir: Path, pages: list[Path], preset: str, run, total_timeout: int,
    ) -> tuple[Path, Path, int | None]:
        """Запустить Audiveris; для однолинейного стана — лесенкой по интервалу.

        Интервал однолинейного стана Audiveris мерить не из чего, его задаём сами
        (оценка по головкам, api/drums.py). Окно у Audiveris узкое: на perc-01
        работают 18-20, а 16-17 роняют его NPE, — поэтому при провале пробуем
        соседние значения, каждое в своём каталоге. Все попытки делят таймаут
        одного прогона.
        """
        if not drums.needs_interline(preset):
            return run(output_dir, [], total_timeout)

        estimate = self._drum_interline(pages)
        if estimate is None:
            raise ProcessingError(
                "Однолинейный стан ударных: не удалось оценить интервал по головкам "
                "нот, а без него Audiveris такой лист не разбирает"
            )
        values = drums.ladder(estimate, settings.min_interline)
        if not values:
            raise ProcessingError(
                f"Однолинейный стан ударных: интервал {estimate}px ниже порога "
                f"{settings.min_interline}px — снимок слишком мелкий"
            )

        started_at = time.monotonic()
        failure: ProcessingError | None = None
        for value in values:
            if failure is not None and time.monotonic() - started_at >= total_timeout:
                break
            target = output_dir / f"interline-{value}"
            target.mkdir(parents=True, exist_ok=True)
            try:
                return run(
                    target,
                    ["-constant", f"{drums.INTERLINE_CONSTANT}={value}"],
                    self._remaining_timeout(started_at, total_timeout),
                )
            except ProcessingError as exc:
                logger.warning("Audiveris с интервалом %spx не справился: %s", value, exc.message)
                failure = exc
        raise failure

    def _drum_interline(self, pages: list[Path]) -> int | None:
        """Интервал по головкам нот; у плейлиста — медиана по страницам."""
        values: list[int] = []
        for page in pages:
            dpi = 0
            if page.suffix.lower() == ".pdf":
                dpi = self._pdf_render_dpi(page) or settings.pdf_render_dpi
            try:
                value = drums.estimate_interline(drums.page_gray(page, dpi))
            except Exception:
                logger.exception("оценка интервала упала на %s", page.name)
                value = None
            if value is not None:
                values.append(value)
        return statistics.median_low(values) if values else None

    def _audiveris_readable(self, input_path: Path, enhance: bool) -> Path:
        """Вход, который Audiveris прочитает: HEIC и WebP перекладываем в растр."""
        if input_path.suffix.lower() in (".heic", ".heif"):
            input_path = self._convert_heif(input_path)
        input_path = self._prepare_input(input_path, enhance)
        if input_path.suffix.lower() not in drums.AUDIVERIS_SUFFIXES:
            raise ProcessingError(
                f"Audiveris не читает формат {input_path.suffix or '(без расширения)'}: "
                f"{input_path.name}"
            )
        return input_path

    @staticmethod
    def _convert_heif(input_path: Path) -> Path:
        """HEIC/HEIF с айфона → PNG (декодер — pillow-heif, как в omr/stages/load.py)."""
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            target = input_path.with_suffix(".png")
            with Image.open(input_path) as img:
                img.convert("RGB").save(target)
            return target
        except Exception as exc:
            raise ProcessingError(
                f"Не удалось открыть HEIC {input_path.name}: {type(exc).__name__}: {exc}"
            ) from exc

    def _run_audiveris(
            self, input_path: Path, output_dir: Path, preset: str = "default",
            extra_args: list[str] | tuple[str, ...] = (),
            timeout_seconds: int | None = None,
    ) -> tuple[Path, Path, int | None]:
        """Run audiveris on a single input file, already prepared (`_audiveris_readable`)."""
        # Build command with preset
        preset_enum = Preset(preset) if preset else Preset.default
        preset_args = [*get_preset_args(preset_enum), *extra_args]

        is_pdf = input_path.suffix.lower() == ".pdf"
        movement_args = [] if is_pdf else self._NO_MOVEMENT_SPLIT

        # PDF: ограничиваем DPI рендера, чтобы страница не превысила лимит Audiveris
        # (иначе «Too large image» → пустой результат). Для не-PDF пусто.
        pdf_args = self._pdf_resolution_args([input_path])

        cmd = [
            settings.audiveris_cmd,
            "-batch",
            "-constant", f"org.audiveris.omr.sheet.ScaleBuilder.minInterline={settings.min_interline}",
            *pdf_args,
            # *movement_args,
            *preset_args,
            "-transcribe", "-export",
            "-output", str(output_dir),
            str(input_path),
        ]
        return self._execute_and_process(
            cmd,
            output_dir,
            timeout_seconds=timeout_seconds or self._timeout_for_single(),
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
            extra_args: list[str] | tuple[str, ...] = (),
            timeout_seconds: int | None = None,
    ) -> tuple[Path, Path, int | None]:
        """Run audiveris with playlist of already prepared inputs (`_audiveris_readable`).

        Step 1: Create compound book from playlist (images -> playlist.omr)
        Step 2: Transcribe and export the compound book
        """
        all_logs: list[str] = []
        total_timeout = timeout_seconds or self._timeout_for_playlist(input_paths)
        started_at = time.monotonic()

        # Build preset args (дополнительные -constant — в оба шага: константа живёт
        # в рамках одного JVM-вызова)
        preset_enum = Preset(preset) if preset else Preset.default
        preset_args = [*get_preset_args(preset_enum), *extra_args]

        processed_paths = list(input_paths)

        # PDF-входы: ограничиваем DPI рендера, иначе Audiveris отбросит крупные
        # листы («Too large image») уже на шаге сборки compound-книги (там PDF и
        # рендерится). Применяем к ОБОИМ шагам — -constant живёт в рамках одного
        # JVM-вызова.
        pdf_args = self._pdf_resolution_args(processed_paths)

        # Step 1: Create compound book from playlist
        playlist_path = self._create_playlist_xml(processed_paths, output_dir)
        cmd_build = [
            settings.audiveris_cmd,
            "-batch",
            "-constant", f"org.audiveris.omr.sheet.ScaleBuilder.minInterline={settings.min_interline}",
            *pdf_args,
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
            *pdf_args,
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
            # Слишком большой лист: Audiveris молча (WARN) отбрасывает страницу и
            # не создаёт партитур — без этой строки ошибка выглядит как «нет выхода».
            elif "Too large image" in line:
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
