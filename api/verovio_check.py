"""Проверка «сыграет ли это мобильное приложение» через verovio.

Приложение открывает MusicXML тем же verovio: сначала грузит файл, потом
раскладывает страницу и рисует её, а для воспроизведения собирает MIDI. Здесь
повторяется ровно этот набор вызовов — он и есть контракт с клиентом.

Почему проверяются ОБА пути, а не только MIDI. `renderToMIDI` обходит
музыкальное содержимое, а `getPageCount`/`renderToSVG` запускают вёрстку —
это другой код verovio со своими падениями. Файл, который собрался в MIDI,
не обязан свёрстаться, и наоборот; проверять надо то, что делает клиент.
Вёрстка стоит порядка сотых долей секунды, так что цена вопроса нулевая.

verovio на невалидном MusicXML (напр. <beam> на ноте-члене аккорда из Audiveris)
не бросает исключение, а **роняет процесс сегфолтом** (exit 139) — иногда прямо
на loadFile. Поэтому проверку нельзя ловить try/except в основном процессе: её
гоняем в отдельном процессе и смотрим код выхода.
"""

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Коды выхода раннера: по ним видно, на чём именно споткнулся файл.
_EXIT_LOAD = 2
_EXIT_MIDI = 3
_EXIT_LAYOUT = 4
_STEP_BY_CODE = {
    _EXIT_LOAD: "loadFile",
    _EXIT_MIDI: "renderToMIDI",
    _EXIT_LAYOUT: "вёрстка (getPageCount/renderToSVG)",
    -11: "СЕГФОЛТ verovio",
    139: "СЕГФОЛТ verovio",
}

_RUNNER = (
    "import sys, verovio\n"
    "tk = verovio.toolkit()\n"
    "if not tk.loadFile(sys.argv[1]):\n"
    f"    sys.exit({_EXIT_LOAD})\n"
    "if not tk.renderToMIDI():\n"
    f"    sys.exit({_EXIT_MIDI})\n"
    "if tk.getPageCount() < 1 or not tk.renderToSVG(1):\n"
    f"    sys.exit({_EXIT_LAYOUT})\n"
    "sys.exit(0)\n"
)


def renders_ok(path: Path, timeout: int = 30) -> bool:
    """True, если verovio грузит файл, собирает MIDI и верстает первую страницу.

    Ровно то, что делает мобильное приложение. Любой сбой, включая сегфолт и
    таймаут, — False.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", _RUNNER, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("verovio check timed out after %ss for %s", timeout, path)
        return False
    except Exception:
        logger.exception("verovio check failed to launch for %s", path)
        return False

    if result.returncode != 0:
        logger.warning(
            "verovio не принял %s: %s (код %s)",
            path.name, _STEP_BY_CODE.get(result.returncode, "неизвестный сбой"),
            result.returncode,
        )
    return result.returncode == 0
