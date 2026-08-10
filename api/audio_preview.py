"""Генерация короткого MP3-превью из MusicXML/MXL.

Конвейер намеренно состоит из отдельных процессов:

1. verovio загружает MusicXML и сохраняет временный MIDI;
2. FluidSynth рендерит MIDI через зафиксированный General MIDI SoundFont;
3. ffmpeg убирает начальную тишину, ограничивает длительность, нормализует
   громкость, добавляет fade-out и кодирует MP3.

Verovio может сегфолтиться на повреждённом MusicXML, поэтому его нельзя вызывать
в процессе API/импортёра. Временные MIDI/WAV/MP3 всегда удаляются автоматически.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from api.config import settings

logger = logging.getLogger(__name__)

_MIDI_RUNNER = (
    "import sys, verovio\n"
    "tk = verovio.toolkit()\n"
    "tk.setOptions({'breaks': 'none'})\n"
    "if not tk.loadFile(sys.argv[1]):\n"
    "    sys.exit(2)\n"
    "sys.exit(0 if tk.renderToMIDIFile(sys.argv[2]) else 3)\n"
)


def _run(cmd: list[str], *, timeout: int, stage: str) -> bool:
    """Запустить этап рендера и вернуть False при любом контролируемом сбое."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(timeout, 1),
        )
    except subprocess.TimeoutExpired:
        logger.warning("audio preview %s timed out after %ss", stage, timeout)
        return False
    except (OSError, ValueError):
        logger.exception("audio preview %s failed to launch", stage)
        return False

    if result.returncode == 0:
        return True

    detail = (result.stderr or result.stdout or "").strip()
    if len(detail) > 1000:
        detail = detail[-1000:]
    logger.warning(
        "audio preview %s failed with exit code %s: %s",
        stage,
        result.returncode,
        detail or "no process output",
    )
    return False


def _ffmpeg_filter(duration: float) -> str:
    fade_duration = min(max(settings.audio_preview_fade_out_seconds, 0.0), duration)
    fade_start = max(duration - fade_duration, 0.0)
    filters = [
        # Начинать превью с первого слышимого события, но не съедать тихую
        # атаку струнных/духовых слишком агрессивным порогом.
        "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-60dB",
        f"loudnorm=I={settings.audio_preview_loudness_lufs:g}:LRA=11:TP=-1.5",
    ]
    if fade_duration > 0:
        filters.append(f"afade=t=out:st={fade_start:g}:d={fade_duration:g}")
    return ",".join(filters)


def render_audio_preview(
    music_path: Path,
    *,
    duration: float | None = None,
    soundfont_path: Path | None = None,
) -> bytes | None:
    """Сгенерировать MP3-превью и вернуть байты или ``None`` при ошибке.

    Функция ничего не пишет рядом с исходником. Все промежуточные файлы живут в
    системной временной директории и удаляются даже при таймауте/падении этапа.
    """
    music_path = Path(music_path)
    if not music_path.is_file():
        logger.warning("audio preview source does not exist: %s", music_path)
        return None

    preview_duration = float(
        duration if duration is not None else settings.audio_preview_duration_seconds
    )
    if preview_duration <= 0:
        logger.warning("audio preview duration must be positive: %s", preview_duration)
        return None

    soundfont = Path(soundfont_path or settings.audio_preview_soundfont_path)
    if not soundfont.is_file():
        logger.warning("audio preview SoundFont does not exist: %s", soundfont)
        return None

    with tempfile.TemporaryDirectory(prefix="audiveris-audio-preview-") as temp_dir:
        work_dir = Path(temp_dir)
        midi_path = work_dir / "score.mid"
        wav_path = work_dir / "score.wav"
        mp3_path = work_dir / "preview.mp3"

        if not _run(
            [sys.executable, "-c", _MIDI_RUNNER, str(music_path), str(midi_path)],
            timeout=settings.audio_preview_timeout_seconds,
            stage="MusicXML-to-MIDI",
        ) or not midi_path.is_file():
            return None

        if not _run(
            [
                settings.audio_preview_fluidsynth_cmd,
                "-ni",
                "-q",
                "-F",
                str(wav_path),
                "-T",
                "wav",
                "-O",
                "s16",
                "-r",
                str(settings.audio_preview_sample_rate),
                str(soundfont),
                str(midi_path),
            ],
            timeout=settings.audio_preview_timeout_seconds,
            stage="MIDI-to-WAV",
        ) or not wav_path.is_file():
            return None

        if not _run(
            [
                settings.audio_preview_ffmpeg_cmd,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(wav_path),
                "-t",
                f"{preview_duration:g}",
                "-af",
                _ffmpeg_filter(preview_duration),
                "-map_metadata",
                "-1",
                "-vn",
                "-ar",
                str(settings.audio_preview_sample_rate),
                "-ac",
                "2",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                f"{settings.audio_preview_bitrate_kbps}k",
                str(mp3_path),
            ],
            timeout=settings.audio_preview_timeout_seconds,
            stage="WAV-to-MP3",
        ) or not mp3_path.is_file():
            return None

        content = mp3_path.read_bytes()
        return content or None


def render_audio_preview_file(
    music_path: Path,
    output_path: Path,
    *,
    duration: float | None = None,
    soundfont_path: Path | None = None,
) -> bool:
    """Сгенерировать превью в ``output_path``; вернуть True при успехе."""
    content = render_audio_preview(
        music_path,
        duration=duration,
        soundfont_path=soundfont_path,
    )
    if not content:
        return False
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    return True
