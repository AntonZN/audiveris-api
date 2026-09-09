"""Растеризация PDF: страница -> картинка, дальше всё идёт общим пайплайном.

Зачем вообще растеризовать, если Audiveris умеет PDF сам. Затем, что он
обрабатывает книгу целиком и **одна страница без нот обнуляет экспорт всей
книги**: на реальном `pdf-02-piano-duet.pdf` листы 1-4 распознались полностью, а
колофон с копирайтом пятым листом встал на шаге SCALE — и на выходе не оказалось
ни одного MusicXML. 102 такта потеряны из-за служебной страницы.

Постраничная растеризация снимает это по построению: каждая страница — отдельная
задача, страница без станов просто пропускается (пайплайн сам её опознаёт, найдя
ноль станов), а остальные доходят до результата.

Рендерер — pypdfium2: разрешительная лицензия и самодостаточные wheel'ы, никаких
системных пакетов в образе (в отличие от poppler/pdf2image).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_MAGIC_PDF = b"%PDF"


class PdfUnavailable(Exception):
    """PDF на входе, а рендерер не установлен."""


@dataclass
class PdfPage:
    number: int          # человеческая нумерация, с 1
    image: Path
    dpi: int


def is_pdf(path: Path) -> bool:
    """PDF определяем по сигнатуре, а не по расширению: имя файла нам не подконтрольно."""
    try:
        with Path(path).open("rb") as handle:
            return handle.read(4) == _MAGIC_PDF
    except OSError:
        return False


def rasterize(
    path: Path,
    out_dir: Path,
    *,
    target_width: int = 1920,
    headroom: float = 1.6,
    max_pages: int | None = None,
    min_dpi: int = 150,
    max_dpi: int = 400,
) -> list[PdfPage]:
    """Отрисовать страницы PDF в PNG. Возвращает описания страниц по порядку.

    DPI не фиксированный, а считается из размера конкретной страницы так, чтобы
    ширина рендера была примерно `headroom` × целевой ширины движка. Запас нужен
    потому, что дальше пайплайн вырежет из страницы блок нот и ужмёт его до
    целевой ширины: рендерить ровно в целевую — значит заставить его потом
    достраивать пиксели, которых нет.

    Фиксированные 300 dpi (как у Audiveris) плохи с двух сторон: на A4 это
    больше, чем движок способен использовать, а на странице нестандартного
    размера — либо мало, либо столько, что лист не влезает в лимиты.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise PdfUnavailable(
            "Для PDF нужен pypdfium2 (pip install pypdfium2)"
        ) from exc

    path, out_dir = Path(path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    document = pdfium.PdfDocument(str(path))
    try:
        total = len(document)
        limit = total if max_pages is None else min(total, max_pages)
        pages: list[PdfPage] = []
        for index in range(limit):
            page = document[index]
            width_points, _ = page.get_size()
            wanted = target_width * headroom
            dpi = int(round(72.0 * wanted / max(width_points, 1.0)))
            dpi = max(min_dpi, min(max_dpi, dpi))
            image = page.render(scale=dpi / 72.0).to_pil()
            target = out_dir / f"page{index + 1:02d}.png"
            image.save(target)
            pages.append(PdfPage(number=index + 1, image=target, dpi=dpi))
        return pages
    finally:
        document.close()
