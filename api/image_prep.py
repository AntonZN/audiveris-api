"""Aggressive image preprocessing for phone photos / screenshots of scores.

Backed by the empirical work in ``preprocessing/`` (see its README). The recipe —
autocrop letterbox borders, upscale, adaptive Gaussian threshold — recovers staff
detection on low-resolution inputs where Audiveris otherwise finds only a fraction of
the staves. Both the upscale (gives small glyphs enough pixels) and the *adaptive*
threshold (keeps thin staff lines continuous; a global Otsu threshold drops them) are
required. Opt-in via the API ``enhance`` flag so clean scans are left untouched.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Tuned on screenshot + clean-scan samples (preprocessing/README.md). The adaptive
# block size scales with the upscale factor; 25 px at native -> ~51 px at 2x.
UPSCALE_FACTOR = 2.0
ADAPTIVE_BLOCK_BASE = 25
ADAPTIVE_C = 5
# Pixels below this are treated as letterbox/background when cropping to the page.
PAGE_THRESHOLD = 60

_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _odd(value: int) -> int:
    return value if value % 2 == 1 else value + 1


def enhance_for_omr(src: Path) -> Path:
    """Apply the photo/screenshot recipe and return the path to the processed image.

    Returns ``src`` unchanged on any failure or for non-raster inputs (e.g. PDF), so
    enabling ``enhance`` can never harder-fail a task than leaving it off.
    """
    if src.suffix.lower() not in _RASTER_SUFFIXES:
        return src

    try:
        import cv2
        import numpy as np
    except Exception:
        logger.warning("enhance requested but opencv/numpy unavailable; skipping")
        return src

    try:
        img = cv2.imread(str(src), cv2.IMREAD_COLOR)
        if img is None:
            return src
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Crop the bright page out of dark/letterbox borders.
        mask = gray > PAGE_THRESHOLD
        cols = np.where(mask.any(axis=0))[0]
        rows = np.where(mask.any(axis=1))[0]
        if len(cols) and len(rows):
            gray = gray[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]

        # Upscale so thin staff lines and small glyphs have enough pixels.
        gray = cv2.resize(
            gray,
            (int(gray.shape[1] * UPSCALE_FACTOR), int(gray.shape[0] * UPSCALE_FACTOR)),
            interpolation=cv2.INTER_CUBIC,
        )

        block = _odd(int(ADAPTIVE_BLOCK_BASE * UPSCALE_FACTOR))
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY,
            block, ADAPTIVE_C,
        )

        out = src.with_name(f"{src.stem}_enhanced.png")
        if not cv2.imwrite(str(out), binary):
            return src
        return out
    except Exception:
        logger.exception("enhance preprocessing failed for %s; using original", src)
        return src
