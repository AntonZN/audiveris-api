"""Обёртка над движком homr: чистая картинка -> MusicXML.

Модуль называется `homr_engine`, а не `homr`, намеренно. Раннер запускается
как скрипт, и Python кладёт его директорию первой в `sys.path` — файл с именем
`homr.py` рядом с ним перекрыл бы настоящий пакет homr, и импорт `homr.main`
падал бы с «'homr' is not a package».

Пайплайн `omr` от движка не зависит и знает о нём ровно одно — что тот принимает
PNG/JPG и кладёт рядом `.musicxml`. Захотим поменять движок (или сравнить два) —
меняется только этот файл.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

_RUNNER = Path(__file__).with_name("_homr_runner.py")


class EngineError(Exception):
    """Движок не смог распознать вход."""


@dataclass
class EngineResult:
    musicxml: Path
    log: Path
    seconds: float
    # Сырые строки OCR над верхним станом (см. _install_tempo_capture в раннере).
    # Тут лежит метрономная отметка — «♩=95», «= 117 "Water"», — которую сам homr
    # в MusicXML не пишет: он её либо отбрасывает, либо чистит до неузнаваемости.
    ocr_texts: list[str] = field(default_factory=list)


def interpreter() -> str:
    """Питон, в котором установлен homr.

    По умолчанию — текущий. Переопределяется переменной `OMR_HOMR_PYTHON`: у
    homr тяжёлые и капризные зависимости (onnxruntime, rapidocr), и держать его в
    отдельном venv — нормальная практика.
    """
    return os.environ.get("OMR_HOMR_PYTHON") or sys.executable


def run(
    image: np.ndarray | Path,
    output_dir: Path,
    *,
    timeout: int = 300,
    stem: str = "page",
) -> EngineResult:
    """Прогнать картинку через homr. Возвращает путь к MusicXML и лог запуска."""
    import time

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    work = output_dir / ".engine"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    # homr пишет результат РЯДОМ со входом, поэтому вход кладём в свой подкаталог.
    source = work / f"{stem}.png"
    if isinstance(image, (str, Path)):
        shutil.copyfile(image, source)
    else:
        cv2.imwrite(str(source), image)

    command = [interpreter(), str(_RUNNER), str(source)]
    started = time.monotonic()
    completed = _run(command, timeout)
    elapsed = time.monotonic() - started

    ocr_texts = _read_ocr_sidecar(source.with_suffix(".ocr.txt"))

    log = output_dir / f"{stem}.engine.log"
    log.write_text(
        f"cmd: {' '.join(command)}\nreturncode: {completed.returncode}\n"
        f"seconds: {elapsed:.1f}\n\n--- stdout ---\n{completed.stdout}"
        f"\n\n--- stderr ---\n{completed.stderr}\n"
        f"\n--- сырой ocr над верхним станом ---\n" + "\n".join(ocr_texts) + "\n"
    )

    produced = source.with_suffix(".musicxml")
    if completed.returncode != 0 or not produced.exists():
        # Таймаут называем таймаутом: это про ресурсы и размер файла, а не про
        # распознавание, и в статистике провалов он должен стоять отдельно.
        if "TIMEOUT after" in (completed.stderr or ""):
            raise EngineError(f"homr не уложился в таймаут ({timeout} c), см. {log}")
        raise EngineError(f"homr не дал MusicXML (код {completed.returncode}), см. {log}")

    result = output_dir / f"{stem}.musicxml"
    shutil.move(str(produced), result)
    shutil.rmtree(work, ignore_errors=True)
    return EngineResult(result, log, elapsed, ocr_texts)


def _read_ocr_sidecar(path: Path) -> list[str]:
    """Прочитать строки, отложенные раннером. Нет файла — homr ничего не отбросил."""
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except OSError:
        return []


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Запуск с таймаутом; по таймауту убиваем всю группу процессов.

    Именно группу: onnxruntime разводит рабочие потоки и дочерние процессы, и
    `process.kill()` оставил бы их висеть.
    """
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(
            command, -1, stdout or "", (stderr or "") + f"\nTIMEOUT after {timeout}s"
        )
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
