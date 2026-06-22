"""Генерация превью (обложки) ноты из MusicXML через verovio.

verovio рендерит первую страницу партитуры в SVG, cairosvg растеризует в PNG.
Рендер verovio вынесен в ОТДЕЛЬНЫЙ процесс: на кривом MusicXML verovio может
ронять процесс сегфолтом (см. api/verovio_check.py), и ловить это в основном
процессе нельзя.
"""

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Дочерний процесс: грузит файл, рендерит первую страницу в SVG, печатает в stdout.
# Опции дают компактную «шапку» партитуры (заголовок + первые системы).
_SVG_RUNNER = (
    "import sys, verovio\n"
    "tk = verovio.toolkit()\n"
    "tk.setOptions({\n"
    "    'pageWidth': 2100,\n"
    "    'pageHeight': 1500,\n"
    "    'scale': 40,\n"
    "    'adjustPageHeight': False,\n"
    "    'footer': 'none',\n"
    "    'header': 'auto',\n"
    "    'pageMarginTop': 50,\n"
    "    'pageMarginBottom': 50,\n"
    "    'pageMarginLeft': 50,\n"
    "    'pageMarginRight': 50,\n"
    "})\n"
    "if not tk.loadFile(sys.argv[1]):\n"
    "    sys.exit(2)\n"
    "svg = tk.renderToSVG(1)\n"
    "if not svg:\n"
    "    sys.exit(3)\n"
    "sys.stdout.write(svg)\n"
)


def _render_svg(music_path: Path, timeout: int) -> str | None:
    """Отрендерить первую страницу MusicXML в SVG (в изолированном процессе)."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", _SVG_RUNNER, str(music_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("verovio SVG render timed out after %ss for %s", timeout, music_path)
        return None
    except Exception:
        logger.exception("verovio SVG render failed to launch for %s", music_path)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout


def render_cover_png(music_path: Path, width: int = 600, timeout: int = 60) -> bytes | None:
    """Сгенерировать PNG-обложку из MusicXML и вернуть её байтами.

    None, если verovio не смог отрендерить файл или cairosvg недоступен/не справился.
    """
    svg = _render_svg(music_path, timeout)
    if not svg:
        return None
    try:
        import cairosvg

        return cairosvg.svg2png(
            bytestring=svg.encode("utf-8"),
            output_width=width,
            background_color="white",
        )
    except Exception:
        logger.exception("cover rasterization failed for %s", music_path)
        return None


def render_cover(music_path: Path, out_png: Path, width: int = 600, timeout: int = 60) -> bool:
    """Сгенерировать PNG-обложку и записать в файл. True при успехе."""
    png = render_cover_png(music_path, width=width, timeout=timeout)
    if not png:
        return False
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_png.write_bytes(png)
    return True
