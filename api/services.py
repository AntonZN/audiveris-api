import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from api.config import settings
from api.exceptions import LowInterlineError, ProcessingError
from api.models import FileResult


class AudiverisService:
    def process_single(self, input_path: Path, output_dir: Path) -> FileResult:
        """Process a single input file and return a FileResult."""
        try:
            output_path, log_path, interline = self._run_audiveris(
                input_path, output_dir
            )
            return FileResult(
                filename=output_path.name,
                url=self._build_media_url(output_path),
                log_url=self._build_media_url(log_path) if log_path else None,
            )
        except LowInterlineError as exc:
            return FileResult(
                filename=input_path.name,
                error=exc.message,
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )
        except ProcessingError as exc:
            return FileResult(
                filename=input_path.name,
                error=exc.message,
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )

    def process_playlist(self, input_paths: list[Path], output_dir: Path) -> FileResult:
        """Process multiple files as a playlist (single book) and return a FileResult."""
        try:
            output_path, log_path, interline = self._run_audiveris_playlist(
                input_paths, output_dir
            )
            return FileResult(
                filename=output_path.name,
                url=self._build_media_url(output_path),
                log_url=self._build_media_url(log_path) if log_path else None,
            )
        except LowInterlineError as exc:
            return FileResult(
                filename="playlist",
                error=exc.message,
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )
        except ProcessingError as exc:
            return FileResult(
                filename="playlist",
                error=exc.message,
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )

    def _run_audiveris(
            self, input_path: Path, output_dir: Path
    ) -> tuple[Path, Path, int | None]:
        """Run audiveris on a single input file."""
        cmd = [
            settings.audiveris_cmd,
            *["-batch", "-transcribe", "-export", "-output"],
            str(output_dir),
            str(input_path),
        ]
        return self._execute_and_process(cmd, output_dir)

    def _execute_and_process(
            self, cmd: list[str], output_dir: Path
    ) -> tuple[Path, Path, int | None]:
        """Execute audiveris command and process results."""
        result = subprocess.run(cmd, capture_output=True, text=True)
        log_path = self._write_log(output_dir, cmd, result)
        book_log = self._find_audiveris_log(output_dir, log_path)
        interline_value = self._detect_interline(book_log)

        if interline_value is not None and interline_value < settings.min_interline:
            detail = (
                f"Image resolution too low: interline={interline_value}px < {settings.min_interline}px"
            )
            raise LowInterlineError(interline_value, detail, book_log)

        if result.returncode != 0:
            error = (result.stderr or result.stdout or "Audiveris failed").strip()
            detail = f"Audiveris failed. {error}"
            raise ProcessingError(detail, log_path=book_log)

        # Check for errors in stdout (Audiveris may return 0 even with errors)
        processing_errors = self._detect_processing_errors(result.stdout or "")

        candidates = self._find_outputs(output_dir)
        if not candidates:
            files = self._list_files(output_dir)
            error_info = f" Errors: {processing_errors}" if processing_errors else ""
            detail = f"No MusicXML output found, files={files}).{error_info}"
            raise ProcessingError(detail, log_path=book_log)

        output_path = sorted(candidates)[0]
        return output_path, book_log, interline_value

    def _create_playlist_xml(self, input_paths: list[Path], output_dir: Path) -> Path:
        """Create a playlist XML file for audiveris."""
        playlist_path = output_dir / "playlist.xml"
        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<play-list>"]
        for path in input_paths:
            lines.append(f"  <excerpt>")
            lines.append(f"    <path>{path}</path>")
            lines.append(f"  </excerpt>")
        lines.append("</play-list>")
        playlist_path.write_text("\n".join(lines))
        return playlist_path

    def _run_audiveris_playlist(
            self, input_paths: list[Path], output_dir: Path
    ) -> tuple[Path, Path, int | None]:
        """Run audiveris with playlist.

        Step 1: Create compound book from playlist (images -> playlist.omr)
        Step 2: Transcribe and export the compound book
        """
        all_logs: list[str] = []

        # Step 1: Create compound book from playlist
        playlist_path = self._create_playlist_xml(input_paths, output_dir)
        cmd_build = [
            settings.audiveris_cmd,
            "-batch",
            "-playlist", str(playlist_path),
            "-output", str(output_dir),
        ]
        result_build = subprocess.run(cmd_build, capture_output=True, text=True)
        all_logs.append(f"=== Step 1: Build compound book ===")
        all_logs.append(f"cmd: {' '.join(cmd_build)}")
        all_logs.append(result_build.stdout or "")
        if result_build.stderr:
            all_logs.append(result_build.stderr)

        # Find compound .omr file
        compound_omr = output_dir / "playlist.omr"
        if not compound_omr.exists():
            log_path = output_dir / "audiveris.log"
            log_path.write_text("\n".join(all_logs))
            raise ProcessingError(
                f"Compound book not created",
                log_path=log_path
            )

        # Step 2: Transcribe and export compound book
        cmd_export = [
            settings.audiveris_cmd,
            "-batch",
            "-transcribe",
            "-export",
            "-output", str(output_dir),
            str(compound_omr),
        ]
        all_logs.append(f"\n=== Step 2: Transcribe and export ===")
        all_logs.append(f"cmd: {' '.join(cmd_export)}")

        # Write intermediate log
        log_path = output_dir / "audiveris.log"
        log_path.write_text("\n".join(all_logs))

        return self._execute_and_process(cmd_export, output_dir)

    def _write_log(
            self, out_dir: Path, cmd: list[str], result: subprocess.CompletedProcess
    ) -> Path:
        """Write audiveris execution log."""
        log_path = out_dir / "audiveris.log"
        payload = [
            f"cmd: {' '.join(cmd)}",
            f"returncode: {result.returncode}",
            "",
            "stdout:",
            result.stdout or "",
            "",
            "stderr:",
            result.stderr or "",
        ]
        log_path.write_text("\n".join(payload))
        return log_path

    def _find_audiveris_log(self, out_dir: Path, fallback: Path) -> Path:
        """Find the audiveris-generated log file."""
        logs = [path for path in out_dir.rglob("*.log") if path.is_file()]
        logs = [path for path in logs if path.name != fallback.name]
        if not logs:
            return fallback
        logs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return logs[0]

    def _detect_interline(self, log_path: Path) -> int | None:
        """Detect interline value from log file."""
        if not log_path.exists():
            return None
        content = log_path.read_text(errors="ignore")
        values = [
            int(match.group(1))
            for match in re.finditer(r"interline value of (\d+) pixels", content)
        ]
        if not values:
            return None
        return min(values)

    def _detect_processing_errors(self, stdout: str) -> list[str]:
        """Detect processing errors from Audiveris stdout."""
        errors = []
        for line in stdout.split("\n"):
            # Look for WARN/ERROR lines with exceptions
            if "Error in performing" in line or "Exception" in line:
                errors.append(line.strip())
            # Look for specific error patterns
            elif "WARN" in line and ("Error" in line or "null" in line.lower()):
                errors.append(line.strip())
        return errors[:5]  # Limit to first 5 errors

    def _find_outputs(self, out_dir: Path) -> list[Path]:
        """Find all MusicXML output files (.mxl preferred)."""
        # Exclude known non-output files
        exclude_names = {"playlist.xml"}

        mxl_files = []
        xml_files = []

        for path in out_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name.lower() in exclude_names:
                continue
            suffix = path.suffix.lower()
            if suffix == ".mxl":
                mxl_files.append(path)
            elif suffix == ".xml":
                # Only include XML files that look like MusicXML (not internal Audiveris files)
                # Audiveris stores internal data in subdirectories like /sheet#1/sheet#1.xml
                if path.parent == out_dir:
                    xml_files.append(path)

        # Prefer .mxl files over .xml
        return mxl_files if mxl_files else xml_files

    def _list_files(self, root: Path) -> str:
        """List files in directory for error messages."""
        if not root.exists():
            return "none"
        files = sorted(
            str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
        )
        if not files:
            return "none"
        if len(files) > settings.max_listed_files:
            return (
                    ", ".join(files[: settings.max_listed_files])
                    + f", ... (+{len(files) - settings.max_listed_files} more)"
            )
        return ", ".join(files)

    def _build_media_url(self, path: Path) -> str | None:
        """Build a media URL for a file path."""
        try:
            media_root = Path(settings.media_root)
            rel = path.relative_to(media_root)
        except ValueError:
            return None

        rel_posix = quote(rel.as_posix())
        base = settings.media_base_url.rstrip("/")
        prefix = settings.media_path_prefix.strip("/")

        if prefix:
            return f"{base}/{prefix}/{rel_posix}"
        return f"{base}/{rel_posix}"


audiveris_service = AudiverisService()
