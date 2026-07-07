"""Распознавание ФОТО нот через homr (трансформерный OMR).

Используется в single-задачах, когда на вход пришёл телефонный снимок (JPEG/HEIC):
homr устойчивее Audiveris к перекосу, шуму и перспективе. Запускается отдельным
процессом в собственном venv, чтобы его зависимости (onnxruntime, rapidocr,
musicxml) не пересекались с зависимостями API. На выходе — сжатый `.mxl`, который
дальше проходит ту же постобработку, что и выход Audiveris
(`AudiverisService._build_success_result`).
"""

import logging
import os
import shutil
import signal
import subprocess
import sys
import zipfile
from pathlib import Path

from PIL import Image

from api.config import settings
from api.exceptions import ProcessingError

logger = logging.getLogger(__name__)

# Раннер homr с монкипатчем от краша на вырожденном стане (см. api/homr_patch.py).
_HOMR_RUNNER = Path(__file__).parent / "homr_patch.py"

# Magic bytes растровых форматов, которые в /single уходят в homr.
_MAGIC_JPEG = b"\xff\xd8\xff"
_MAGIC_PNG = b"\x89PNG\r\n\x1a\n"
_MAGIC_RIFF = b"RIFF"  # WebP = "RIFF"<size>"WEBP"
# HEIC/HEIF: байты 4..12 вида "ftyp<brand>", brand ∈ {heic,heix,heif,mif1,...}.
_HEIF_BRANDS = {b"heic", b"heix", b"heif", b"hevc", b"mif1", b"msf1"}


def is_photo(path: Path) -> bool:
    """True для растрового изображения (JPEG/HEIC/PNG/WebP) — такие идут в homr.

    Только PDF остаётся за Audiveris. Имя историческое: «photo» здесь = любое
    одиночное изображение, не PDF.
    """
    try:
        with path.open("rb") as f:
            header = f.read(12)
    except OSError:
        return False

    if header.startswith(_MAGIC_JPEG):
        return True
    if header.startswith(_MAGIC_PNG):
        return True
    if header.startswith(_MAGIC_RIFF) and header[8:12] == b"WEBP":
        return True
    if header[4:8] == b"ftyp" and header[8:12] in _HEIF_BRANDS:
        return True
    return False


def _is_webp(path: Path) -> bool:
    """True, если файл — WebP (по magic bytes, независимо от расширения)."""
    try:
        with path.open("rb") as f:
            header = f.read(12)
    except OSError:
        return False
    return header.startswith(_MAGIC_RIFF) and header[8:12] == b"WEBP"


def _pack_mxl(musicxml_path: Path, mxl_path: Path) -> None:
    """Упаковать .musicxml в сжатый контейнер .mxl (zip + META-INF/container.xml)."""
    inner = musicxml_path.name
    container = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<container><rootfiles>"
        f'<rootfile full-path="{inner}" '
        'media-type="application/vnd.recordare.musicxml+xml"/>'
        "</rootfiles></container>"
    )
    with zipfile.ZipFile(mxl_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype первым и без сжатия — по спецификации MusicXML-контейнера.
        zf.writestr(
            "mimetype",
            "application/vnd.recordare.musicxml+xml",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("META-INF/container.xml", container)
        zf.write(musicxml_path, inner)


class HomrService:
    """Запуск homr как внешнего процесса и упаковка результата в .mxl."""

    def _write_log(
        self, out_dir: Path, cmd: list[str], result: subprocess.CompletedProcess
    ) -> Path:
        """Записать лог запуска homr (одноимённо с audiveris.log по смыслу)."""
        log_path = out_dir / "homr.log"
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

    def run(self, input_path: Path, output_dir: Path) -> tuple[Path, Path]:
        """Распознать одно фото через homr.

        Returns:
            (mxl_path, log_path) — путь к сжатому MusicXML и к логу запуска.
        Raises:
            ProcessingError — homr упал, не уложился в таймаут или не дал выход.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        # homr пишет <stem>.musicxml рядом с входным файлом, поэтому работаем во
        # временном подкаталоге, а наружу выносим только готовый .mxl.
        work_dir = output_dir / ".homr_work"
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        # WebP homr читает ненадёжно — приводим к JPG (как и Audiveris-пайплайн).
        if input_path.suffix.lower() == ".webp" or _is_webp(input_path):
            work_img = work_dir / f"{input_path.stem}.jpg"
            with Image.open(input_path) as img:
                img.convert("RGB").save(work_img, "JPEG", quality=95)
        else:
            work_img = work_dir / input_path.name
            shutil.copyfile(input_path, work_img)

        # homr — обычная зависимость в этом же venv; зовём его тем же интерпретатором.
        # homr пишет <stem>.musicxml рядом с переданным (абсолютным) путём к картинке.
        # Через homr_patch.py: он монкипатчит краш homr на вырожденном стане
        # (cv2.resize с нулевым размером канвы) и делегирует в homr.main. Запуск по
        # АБСОЛЮТНОМУ пути — не зависим от CWD подпроцесса.
        cmd = [sys.executable, str(_HOMR_RUNNER), str(work_img)]
        timeout = max(settings.homr_timeout_seconds, 1)

        result = self._run_command(cmd, timeout, output_dir)

        produced = work_img.with_suffix(".musicxml")
        if result.returncode != 0 or not produced.exists():
            log_path = self._write_log(output_dir, cmd, result)
            shutil.rmtree(work_dir, ignore_errors=True)
            raise ProcessingError(
                "homr не смог распознать фото (нет MusicXML на выходе)",
                log_path=log_path,
            )

        mxl_path = output_dir / f"{input_path.stem}.mxl"
        _pack_mxl(produced, mxl_path)

        log_path = self._write_log(output_dir, cmd, result)
        shutil.rmtree(work_dir, ignore_errors=True)
        return mxl_path, log_path

    def _run_command(
        self,
        cmd: list[str],
        timeout_seconds: int,
        output_dir: Path,
    ) -> subprocess.CompletedProcess:
        """Запустить homr с таймаутом; при превышении — убить группу процессов."""
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
                cmd, returncode=process.returncode, stdout=stdout, stderr=stderr
            )
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            result = subprocess.CompletedProcess(
                cmd, returncode=-1, stdout=exc.stdout or "", stderr=exc.stderr or ""
            )
            log_path = self._write_log(output_dir, cmd, result)
            raise ProcessingError(
                f"homr timed out after {timeout_seconds} seconds",
                log_path=log_path,
            ) from exc


homr_service = HomrService()
