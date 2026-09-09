"""Пайплайн подготовки фотографий нот к оптическому распознаванию.

    from omr import prepare, recognize

    result = prepare("photo.jpg")      # ровный лист + протокол стадий
    print(result.report())

    score = recognize("photo.jpg", Path("out"))   # ... и сразу MusicXML

Подробности — в omr/README.md.
"""

from omr.config import DEFAULT, PipelineConfig
from omr.pipeline import PrepareResult, StageReport, prepare
from omr.recognize import RecognizeResult, recognize

__all__ = [
    "DEFAULT",
    "PipelineConfig",
    "PrepareResult",
    "RecognizeResult",
    "StageReport",
    "prepare",
    "recognize",
]
