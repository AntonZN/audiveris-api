"""Проверка «соберётся ли MusicXML в MIDI» через verovio.

verovio на невалидном MusicXML (напр. <beam> на ноте-члене аккорда из Audiveris)
не бросает исключение, а **роняет процесс сегфолтом** (exit 139) прямо в
renderToMIDIFile. Поэтому проверку нельзя ловить try/except в основном процессе —
её гоняем в отдельном процессе и смотрим код выхода: 0 = ок, любой другой = не ок.
"""

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Запускается в дочернем процессе: грузит файл и зовёт renderToMIDI() — ровно тот
# вызов, что делает мобильное приложение. Любой краш/сбой даёт ненулевой exit,
# который и считаем сигналом «не ок».
_RUNNER = (
    "import sys, verovio\n"
    "tk = verovio.toolkit()\n"
    "if not tk.loadFile(sys.argv[1]):\n"
    "    sys.exit(2)\n"
    "sys.exit(0 if tk.renderToMIDI() else 3)\n"
)


def midi_ok(path: Path, timeout: int = 30) -> bool:
    """True, если verovio смог собрать MIDI из файла (в изолированном процессе)."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", _RUNNER, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("verovio MIDI check timed out after %ss for %s", timeout, path)
        return False
    except Exception:
        logger.exception("verovio MIDI check failed to launch for %s", path)
        return False
    return result.returncode == 0
