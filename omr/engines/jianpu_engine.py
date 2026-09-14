"""Движок цзянпу: OMR из jpeditor (github.com/lodebar2026/jpeditor, MIT).

Внешний процесс на Node — так же, как homr внешний процесс на Python. Пайплайн
знает о движке ровно то, что тот читает PNG/JPG и отдаёт MusicXML.

Своего распознавания цзянпу у нас нет, а у jpeditor оно рабочее: геометрия
связных компонент плюс PaddleOCR v6 для цифр и текста. Замер на эталонах
`tests/images/jianpu/synth` — в omr/jianpu/README.md.

Пакет собирает `scripts/build_jpeditor_omr.sh`. Где искать: `OMR_JIANPU_CLI`,
иначе /opt/jpeditor-omr (образ), иначе vendor/jpeditor-omr (разработка). Node —
`OMR_NODE`, по умолчанию `node` из PATH.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from omr.engines.homr_engine import EngineError, _run

_CANDIDATES = (
    Path("/opt/jpeditor-omr/omr-cli.mjs"),
    Path(__file__).resolve().parents[2] / "vendor" / "jpeditor-omr" / "omr-cli.mjs",
)
# Что декодирует sharp внутри движка. HEIC и PDF он не читает — их готовит вызывающий.
SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"})
# Так движок сообщает о картинке, которую не разобрал: «✗ page002.png: причина».
_FAILURE = re.compile(r"^✗ (?P<name>[^:]+): (?P<reason>.*)$", re.M)


@dataclass
class PagesResult:
    outputs: dict[str, Path] = field(default_factory=dict)   # имя страницы -> MusicXML
    errors: dict[str, str] = field(default_factory=dict)     # имя страницы -> причина
    log: Path | None = None
    seconds: float = 0.0


def cli() -> Path | None:
    configured = os.environ.get("OMR_JIANPU_CLI")
    if configured:
        return Path(configured)
    return next((path for path in _CANDIDATES if path.exists()), None)


def run_pages(
    pages: list[tuple[Path, str]],
    output_dir: Path,
    *,
    timeout: int = 300,
    log_name: str = "jianpu.engine.log",
) -> PagesResult:
    """Распознать картинки одним запуском. `pages` — (картинка, имя выхода без суффикса).

    Один запуск на все страницы, а не по запуску на страницу: загрузка модели и
    прогрев ONNX стоят ~2.4 с, а сама страница — ~0.3 с.

    Картинка, которую движок не разобрал, попадает в `errors`, и что делать с
    остальными страницами, решает вызывающий. EngineError — только когда не
    отработал сам запуск: движка нет, таймаут, процесс упал, не дав ни одного выхода.
    """
    script = cli()
    if script is None or not script.exists():
        raise EngineError("движок цзянпу не найден: соберите его scripts/build_jpeditor_omr.sh "
                          "или укажите OMR_JIANPU_CLI")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    work = output_dir / ".jianpu"
    shutil.rmtree(work, ignore_errors=True)
    (work / "out").mkdir(parents=True)

    # Выход движок называет по имени входа, поэтому входы кладём под номерами:
    # два снимка «image0.png» из разных папок иначе затёрли бы друг друга.
    staged = []
    for index, (image, _) in enumerate(pages, start=1):
        suffix = Path(image).suffix.lower()
        if suffix not in SUFFIXES:
            shutil.rmtree(work, ignore_errors=True)
            raise EngineError(f"движок цзянпу не читает {suffix or 'файлы без расширения'}")
        copy = work / f"page{index:03d}{suffix}"
        shutil.copyfile(image, copy)
        staged.append(copy)

    command = [os.environ.get("OMR_NODE") or "node", str(script), *map(str, staged),
               "-f", "jpwabc", "-o", str(work / "out")]
    started = time.monotonic()
    try:
        completed = _run(command, timeout)
    except OSError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise EngineError(f"движок цзянпу не запустился: {exc}") from exc
    result = PagesResult(log=output_dir / log_name, seconds=time.monotonic() - started)
    stderr = completed.stderr or ""
    result.log.write_text(
        f"cmd: {' '.join(command)}\nreturncode: {completed.returncode}\n"
        f"seconds: {result.seconds:.1f}\n\n--- stdout ---\n{completed.stdout}"
        f"\n--- stderr ---\n{stderr}\n",
        encoding="utf-8",
    )
    if "TIMEOUT after" in stderr:
        shutil.rmtree(work, ignore_errors=True)
        raise EngineError(f"движок цзянпу не уложился в таймаут ({timeout} с), см. {result.log}")

    failures = {match["name"]: match["reason"].strip() for match in _FAILURE.finditer(stderr)}
    for (_, name), copy in zip(pages, staged):
        produced = work / "out" / f"{copy.stem}.musicxml"
        if produced.exists() and produced.stat().st_size:
            target = output_dir / f"{name}.musicxml"
            shutil.move(str(produced), target)
            result.outputs[name] = target
        else:
            result.errors[name] = failures.get(copy.name) or \
                f"движок не дал MusicXML (код {completed.returncode})"
    shutil.rmtree(work, ignore_errors=True)
    if not result.outputs and not failures:
        raise EngineError(f"движок цзянпу упал (код {completed.returncode}), см. {result.log}")
    return result
