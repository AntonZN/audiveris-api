from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.audio_preview import render_audio_preview
from api.config import settings


class AudioPreviewTest(unittest.TestCase):
    def test_pipeline_returns_mp3_and_uses_expected_limits(self) -> None:
        calls: list[tuple[list[str], dict]] = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[0] == "python-for-test":
                Path(cmd[-1]).write_bytes(b"midi")
            elif cmd[0] == "fluidsynth-for-test":
                Path(cmd[cmd.index("-F") + 1]).write_bytes(b"wav")
            elif cmd[0] == "ffmpeg-for-test":
                Path(cmd[-1]).write_bytes(b"ID3-preview")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            music = root / "score.mxl"
            soundfont = root / "soundfont.sf2"
            music.write_bytes(b"musicxml")
            soundfont.write_bytes(b"soundfont")

            with (
                patch("api.audio_preview.sys.executable", "python-for-test"),
                patch("api.audio_preview.subprocess.run", side_effect=fake_run),
                patch.object(
                    settings,
                    "audio_preview_fluidsynth_cmd",
                    "fluidsynth-for-test",
                ),
                patch.object(settings, "audio_preview_ffmpeg_cmd", "ffmpeg-for-test"),
            ):
                result = render_audio_preview(
                    music,
                    duration=20,
                    soundfont_path=soundfont,
                )

        self.assertEqual(result, b"ID3-preview")
        self.assertEqual(len(calls), 3)
        ffmpeg_cmd = calls[2][0]
        self.assertIn("-t", ffmpeg_cmd)
        self.assertEqual(ffmpeg_cmd[ffmpeg_cmd.index("-t") + 1], "20")
        filters = ffmpeg_cmd[ffmpeg_cmd.index("-af") + 1]
        self.assertIn("loudnorm=I=-16", filters)
        self.assertIn("afade=t=out:st=18.5:d=1.5", filters)
        for _, kwargs in calls:
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertEqual(kwargs["timeout"], 120)

    def test_missing_soundfont_fails_before_spawning_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            music = Path(temp_dir) / "score.mxl"
            music.write_bytes(b"musicxml")
            with patch("api.audio_preview.subprocess.run") as run:
                result = render_audio_preview(
                    music,
                    soundfont_path=Path(temp_dir) / "missing.sf2",
                )
        self.assertIsNone(result)
        run.assert_not_called()

    def test_failed_verovio_stage_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            music = root / "score.mxl"
            soundfont = root / "soundfont.sf2"
            music.write_bytes(b"musicxml")
            soundfont.write_bytes(b"soundfont")
            failed = subprocess.CompletedProcess(["python"], 3, "", "bad score")
            with patch("api.audio_preview.subprocess.run", return_value=failed) as run:
                result = render_audio_preview(music, soundfont_path=soundfont)
        self.assertIsNone(result)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
