"""Распознавание страницы движком Zeus: страница -> кропы станов -> MusicXML.

Zeus (https://github.com/OmniOMR/zeus, ICDAR 2024) читает ОДИН стан за раз и
ждёт на входе «cropped and roughly deskewed single staff». Полную страницу ему
давать нельзя — получится мусор. Поэтому здесь он стыкуется с пакетом `omr`,
который как раз и умеет привести снимок к такому виду: выпрямить страницу,
найти станы и нарезать их (`omr.crop`).

Разделение обязанностей ровно то же, что и с homr: `omr` отвечает за геометрию и
за то, что подаётся движку, движок — только за ноты. Отличие в том, что homr
ищет станы сам, а Zeus полагается на наши.

Живёт отдельным модулем со своим образом, потому что Zeus требует Python 3.10 и
TensorFlow 2.12 — в окружение API это тащить незачем.

Каталог называется `zeus_eval`, а НЕ `zeus`, намеренно: пакет с именем `zeus`
на PYTHONPATH перекрыл бы саму библиотеку Zeus, и её CLI падал бы с
«No module named 'zeus.cli'». Ровно та же ловушка, что была с
`omr/engines/homr.py` и пакетом homr.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from statistics import median

import cv2

from omr import staff
from omr.config import DEFAULT
from omr.merge import merge
from omr.pipeline import prepare
from omr.stages import load
from zeus_eval import systems as systems_stage


def transcribe(
    source: Path,
    output_dir: Path,
    model: Path,
    *,
    keep_crops: bool = False,
) -> dict:
    """Распознать одну страницу. Возвращает отчёт."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"source": source.name}

    started = time.monotonic()
    prepared = prepare(source, DEFAULT)
    report["staves_as_shot"] = prepared.staves_as_shot
    report["staves"] = prepared.staves_after
    report["prepare_seconds"] = round(time.monotonic() - started, 1)

    gray = load.to_gray(prepared.image)
    lines, staves, interline = staff.analyse(gray)
    systems = systems_stage.group_into_systems(staves, staff.ink_mask(gray))
    crops = systems_stage.crop_systems(prepared.image, systems)
    report["crops"] = len(crops)
    report["systems"] = [item.staves for item in crops]
    if not crops:
        report["error"] = "станов не найдено — нечего отдавать движку"
        return report

    crops_dir = output_dir / f"{source.stem}.staves"
    if crops_dir.exists():
        shutil.rmtree(crops_dir)
    crops_dir.mkdir(parents=True)
    images = []
    for item in crops:
        path = crops_dir / f"staff{item.index:02d}.png"
        cv2.imwrite(str(path), item.image)
        images.append(path)

    started = time.monotonic()
    result = subprocess.run(
        ["zeus", "predict", "--model-snapshot", str(model), "--quiet-tf",
         *[str(p) for p in images]],
        capture_output=True, text=True,
    )
    report["engine_seconds"] = round(time.monotonic() - started, 1)
    (output_dir / f"{source.stem}.zeus.log").write_text(
        f"returncode: {result.returncode}\n\n--- stdout ---\n{result.stdout}"
        f"\n\n--- stderr ---\n{result.stderr}\n"
    )

    produced = sorted(crops_dir.glob("*.musicxml"))
    report["transcribed"] = len(produced)
    if not produced:
        report["error"] = f"zeus не дал MusicXML (код {result.returncode})"
        return report

    runaways = _clamp_runaway_staves(produced)
    if runaways:
        report["runaway_staves"] = runaways

    # Станы одной страницы — это системы одной партии, идущие подряд. Склеиваем
    # их тем же кодом, что и страницы: операция ровно та же — дописать такты в
    # хвост партии и перенумеровать.
    target = output_dir / f"{source.stem}.musicxml"
    merged = merge(produced, target)
    report["measures"] = merged.measures
    report["output"] = str(target)

    if not keep_crops:
        shutil.rmtree(crops_dir, ignore_errors=True)
    return report


def _measure_stats(path: Path) -> tuple[int, int]:
    """(тактов, из них пустых) в транскрипции одного стана."""
    part = ET.parse(path).getroot().find("part")
    if part is None:
        return 0, 0
    measures = part.findall("measure")
    empty = sum(1 for m in measures if next(m.iter("pitch"), None) is None)
    return len(measures), empty


def _clamp_runaway_staves(paths: list[Path], factor: int = 4, floor: int = 10) -> list[str]:
    """Обрезать станы, на которых модель сорвалась в цикл.

    Наблюдённый случай: кроп с МНОГОТАКТОВОЙ ПАУЗОЙ (толстая полоса и число
    сверху) — нот нет вообще, вход для модели незнакомый, и декодер уходит в
    петлю: 174 совершенно одинаковых пустых такта при 4-6 у соседних станов.
    Один такой стан раздувает склеенную страницу с 38 тактов до 206.

    Признак срыва — сочетание двух вещей: тактов кратно больше, чем у соседей, И
    почти все они пустые. Одного лишь «много тактов» мало: плотная система
    честно бывает длиннее прочих.

    Лечим обрезкой до медианы по соседям, а не удалением: стан действительно
    состоит из пауз, и выбросить его целиком значит сдвинуть всю музыку после
    него. Длина — заведомо догадка, поэтому она попадает в отчёт.
    """
    stats = {path: _measure_stats(path) for path in paths}
    counts = [count for count, _ in stats.values() if count]
    if len(counts) < 3:
        return []
    normal = median(counts)
    limit = max(int(normal * factor), int(normal) + floor)

    clamped: list[str] = []
    for path, (count, empty) in stats.items():
        if count <= limit or empty < count * 0.7:
            continue
        tree = ET.parse(path)
        part = tree.getroot().find("part")
        measures = part.findall("measure")
        for measure in measures[int(normal):]:
            part.remove(measure)
        tree.write(path, encoding="utf-8", xml_declaration=True)
        clamped.append(f"{path.stem}: {count} тактов (пустых {empty}) -> {int(normal)}")
    return clamped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zeus-transcribe",
        description="Распознать ноты движком Zeus через подготовку пакетом omr",
    )
    parser.add_argument("inputs", nargs="+", help="картинки страниц")
    parser.add_argument("-o", "--output", default="zeus-out")
    parser.add_argument("-m", "--model", default="/models/ayce-2026-08-03.model",
                        help="папка снапшота Zeus (.model)")
    parser.add_argument("--keep-crops", action="store_true",
                        help="не удалять кропы станов (для разбора полётов)")
    args = parser.parse_args(argv)

    output = Path(args.output)
    model = Path(args.model)
    if not model.exists():
        print(f"нет снапшота модели: {model}", file=sys.stderr)
        return 2

    failures = 0
    for name in args.inputs:
        path = Path(name)
        try:
            report = transcribe(path, output, model, keep_crops=args.keep_crops)
        except Exception as exc:  # noqa: BLE001 — одна страница не должна ронять пачку
            failures += 1
            print(f"{path.name}: ИСКЛЮЧЕНИЕ {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if "error" in report:
            failures += 1
            print(f"{path.name}: ОШИБКА — {report['error']}", file=sys.stderr)
        else:
            print(
                f"{path.name}: станов={report['staves']} систем={report['crops']}"
                f"{'/грандстанов ' + str(sum(1 for n in report['systems'] if n > 1)) if any(n > 1 for n in report.get('systems', [])) else ''} "
                f"распознано={report['transcribed']} тактов={report['measures']} "
                f"({report['prepare_seconds']}s подготовка + {report['engine_seconds']}s движок)"
            )
            for note in report.get("runaway_staves", []):
                print(f"    срыв декодера обрезан — {note}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
