"""Audiveris как второй движок — ради символов, которых homr не знает.

homr 0.6.2 не выдаёт динамику (pp…ff, sf), вилки cresc/dim, октавные линии и
педаль: в его словаре их нет, это закомментировано в самой модели. Audiveris их
находит, и находит аккуратно (на эталонах `tests/images/symbols` вилки — ни
одной ложной), но ноты читает заметно хуже. Поэтому ноты остаются за homr, а у
Audiveris берём только ремарки — см. `omr/transplant.py`.

Запускаем на тех же картинках страниц, что ушли в homr, и ПОСТРАНИЧНО: PDF
целиком Audiveris обрабатывает книгой, и одна страница без нот (обложка
mbeach8gtrs, «No regularly spaced lines found») роняет экспорт всей книги.
Картинки идут одним вызовом — JVM поднимается один раз, а каждая картинка всё
равно отдельная книга со своим провалом.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


def command() -> str | None:
    """Команда запуска Audiveris или None, если его нет.

    `OMR_AUDIVERIS_CMD` — явный путь (в API туда кладётся `settings.audiveris_cmd`);
    иначе ищем `audiveris` в PATH, как его ставит Docker-образ.
    """
    explicit = os.environ.get("OMR_AUDIVERIS_CMD")
    if explicit:
        if Path(explicit).exists():
            return explicit
        return shutil.which(explicit)
    return shutil.which("audiveris") or shutil.which("Audiveris")


# Audiveris на растре режет страницу на «части» по отступу системы: у Бетховена
# (стр. с шестью системами) выходило шесть файлов page001.mvt1..mvt6.mxl, и
# символы брались только из первого. Для картинок отступы не значат смену пьесы.
_NO_MOVEMENT_SPLIT = ["-constant", "org.audiveris.omr.sheet.ProcessingSwitches.indentations=false"]


@dataclass
class AudiverisRun:
    # картинка -> её .mxl (обычно один; несколько, если Audiveris всё же поделил)
    outputs: dict[Path, list[Path]] = field(default_factory=dict)
    log: Path | None = None
    seconds: float = 0.0
    returncode: int | None = None
    error: str = ""


def run_pages(images: list[Path], output_dir: Path, *, timeout: int) -> AudiverisRun:
    """Прогнать картинки страниц через Audiveris. Нет Audiveris — пустой результат."""
    result = AudiverisRun()
    executable = command()
    if executable is None:
        result.error = "Audiveris не найден (OMR_AUDIVERIS_CMD / PATH)"
        return result
    if not images:
        return result

    output_dir = Path(output_dir)
    work = output_dir / ".audiveris"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    # Имена — простые: Audiveris называет книгу по файлу, а длинные тире и
    # пробелы в именах уже ломали нам распаковку его вывода.
    inputs: dict[Path, Path] = {}
    for index, image in enumerate(images, start=1):
        target = work / f"page{index:03d}{Path(image).suffix.lower() or '.png'}"
        shutil.copyfile(image, target)
        inputs[target] = Path(image)

    commandline = [executable, "-batch", *_NO_MOVEMENT_SPLIT, "-transcribe", "-export",
                   "-output", str(work), *map(str, inputs)]
    environment = dict(os.environ)
    # Без headless JVM Audiveris на macOS не выходит после экспорта (висит
    # AWT-Shutdown); на Linux без DISPLAY это и так так, флаг безвреден.
    java_options = environment.get("JAVA_OPTS", "")
    if "java.awt.headless" not in java_options:
        environment["JAVA_OPTS"] = (java_options + " -Djava.awt.headless=true").strip()

    started = time.monotonic()
    completed = _run(commandline, timeout, environment)
    result.seconds = time.monotonic() - started
    result.returncode = completed.returncode

    for target, image in inputs.items():
        produced = sorted(work.rglob(f"{target.stem}.mxl")) + sorted(work.rglob(f"{target.stem}.mvt*.mxl"))
        for index, path in enumerate(produced, start=1):
            final = output_dir / f"{target.stem}.{index}.audiveris.mxl"
            shutil.move(str(path), final)
            result.outputs.setdefault(image, []).append(final)

    result.log = output_dir / "audiveris.log"
    result.log.write_text(
        f"cmd: {' '.join(commandline)}\nreturncode: {completed.returncode}\n"
        f"seconds: {result.seconds:.1f}\n\n--- stdout ---\n{_tail(completed.stdout)}"
        f"\n\n--- stderr ---\n{_tail(completed.stderr)}\n",
        encoding="utf-8",
    )
    if "TIMEOUT after" in (completed.stderr or ""):
        result.error = f"Audiveris не уложился в {timeout} c"
    shutil.rmtree(work, ignore_errors=True)
    return result


def _tail(text: str | None, limit: int = 20000) -> str:
    text = text or ""
    return text if len(text) <= limit else "…\n" + text[-limit:]


def _run(commandline: list[str], timeout: int, environment: dict) -> subprocess.CompletedProcess:
    """Запуск с таймаутом; по таймауту убиваем всю группу процессов JVM."""
    process = subprocess.Popen(
        commandline, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True, env=environment,
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
            commandline, -1, stdout or "", (stderr or "") + f"\nTIMEOUT after {timeout}s"
        )
    return subprocess.CompletedProcess(commandline, process.returncode, stdout, stderr)
