import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from api.config import settings
from api.exceptions import LowInterlineError, ProcessingError
from api.models import FileResult


class AudiverisService:
    def __init__(self) -> None:
        self._audiveris_args = settings.audiveris_args.split()

    def process_single(self, input_path: Path, output_dir: Path) -> FileResult:
        """Process a single input file and return a FileResult."""
        try:
            output_path, log_path, interline = self._run_audiveris(
                input_path, output_dir, input_path.stem
            )
            return FileResult(
                filename=output_path.name,
                url=self._build_media_url(output_path),
                interline=interline,
                log_url=self._build_media_url(log_path) if log_path else None,
            )
        except LowInterlineError as exc:
            return FileResult(
                filename=input_path.name,
                error=exc.message,
                error_code="low_interline",
                interline=exc.interline,
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )
        except ProcessingError as exc:
            return FileResult(
                filename=input_path.name,
                error=exc.message,
                error_code="processing_failed",
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )

    def process_playlist(self, input_paths: list[Path], output_dir: Path) -> FileResult:
        """Process multiple files as a playlist (single book) and return a FileResult."""
        try:
            output_path, log_path, interline = self._run_audiveris_playlist(
                input_paths, output_dir, "playlist"
            )
            return FileResult(
                filename=output_path.name,
                url=self._build_media_url(output_path),
                interline=interline,
                log_url=self._build_media_url(log_path) if log_path else None,
            )
        except LowInterlineError as exc:
            return FileResult(
                filename="playlist",
                error=exc.message,
                error_code="low_interline",
                interline=exc.interline,
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )
        except ProcessingError as exc:
            return FileResult(
                filename="playlist",
                error=exc.message,
                error_code="processing_failed",
                log_url=self._build_media_url(exc.log_path) if exc.log_path else None,
            )

    def _run_audiveris(
        self, input_path: Path, output_dir: Path, job_id: str
    ) -> tuple[Path, Path, int | None]:
        """Run audiveris on a single input file."""
        cmd = [
            settings.audiveris_cmd,
            *self._audiveris_args,
            "-output",
            str(output_dir),
            str(input_path),
        ]
        return self._execute_and_process(cmd, output_dir, job_id)

    def _run_audiveris_playlist(
        self, input_paths: list[Path], output_dir: Path, job_id: str
    ) -> tuple[Path, Path, int | None]:
        """Run audiveris with playlist (three-step process with .omr files)."""
        omr_files: list[Path] = []
        all_logs: list[str] = []

        # Step 1: Process each image individually to .omr
        for i, input_path in enumerate(input_paths):
            cmd_transcribe = [
                settings.audiveris_cmd,
                "-batch",
                "-transcribe",
                "-output",
                str(output_dir),
                str(input_path),
            ]
            result = subprocess.run(cmd_transcribe, capture_output=True, text=True)
            all_logs.append(f"=== Processing {input_path.name} ===")
            all_logs.append(result.stdout or "")
            if result.stderr:
                all_logs.append(result.stderr)

            # Find the created .omr file
            omr_file = output_dir / f"{input_path.stem}.omr"
            if omr_file.exists():
                omr_files.append(omr_file)
            else:
                # Try to find it with different name
                found = list(output_dir.glob(f"{input_path.stem}*.omr"))
                if found:
                    omr_files.append(found[0])

        # Write combined log
        log_path = output_dir / "audiveris.log"
        log_path.write_text("\n".join(all_logs))

        if not omr_files:
            detail = f"No .omr files created from images (job_id={job_id})"
            raise ProcessingError(detail, log_path=log_path)

        # Step 2: Create playlist with .omr files and build compound book
        playlist_path = self._create_playlist_xml(omr_files, output_dir)
        cmd_build = [
            settings.audiveris_cmd,
            "-batch",
            "-playlist",
            str(playlist_path),
            "-output",
            str(output_dir),
        ]
        result_build = subprocess.run(cmd_build, capture_output=True, text=True)
        all_logs.append("=== Building compound book ===")
        all_logs.append(result_build.stdout or "")
        if result_build.stderr:
            all_logs.append(result_build.stderr)
        log_path.write_text("\n".join(all_logs))

        if result_build.returncode != 0:
            error = (result_build.stderr or result_build.stdout or "Playlist build failed").strip()
            detail = f"Audiveris playlist build failed (job_id={job_id}). {error}"
            raise ProcessingError(detail, log_path=log_path)

        # Find the compound .omr file
        compound_omr = output_dir / "playlist.omr"
        if not compound_omr.exists():
            omr_candidates = [f for f in output_dir.glob("*.omr") if f not in omr_files]
            if not omr_candidates:
                detail = f"No compound .omr file created (job_id={job_id})"
                raise ProcessingError(detail, log_path=log_path)
            compound_omr = omr_candidates[0]

        # Step 3: Export the compound book to MusicXML
        cmd_export = [
            settings.audiveris_cmd,
            "-batch",
            "-export",
            "-output",
            str(output_dir),
            str(compound_omr),
        ]
        return self._execute_and_process(cmd_export, output_dir, job_id)

    def _execute_and_process(
        self, cmd: list[str], output_dir: Path, job_id: str
    ) -> tuple[Path, Path, int | None]:
        """Execute audiveris command and process results."""
        result = subprocess.run(cmd, capture_output=True, text=True)
        log_path = self._write_log(output_dir, cmd, result)
        book_log = self._find_audiveris_log(output_dir, log_path)
        interline_value = self._detect_interline(book_log)

        if interline_value is not None and interline_value < settings.min_interline:
            detail = (
                f"Image resolution too low: interline={interline_value}px < {settings.min_interline}px "
                f"(job_id={job_id}, log={book_log})"
            )
            raise LowInterlineError(interline_value, detail, book_log)

        if result.returncode != 0:
            error = (result.stderr or result.stdout or "Audiveris failed").strip()
            detail = f"Audiveris failed (job_id={job_id}). {error}"
            raise ProcessingError(detail, log_path=book_log)

        # Check for errors in stdout (Audiveris may return 0 even with errors)
        processing_errors = self._detect_processing_errors(result.stdout or "")

        candidates = self._find_outputs(output_dir)
        if not candidates:
            files = self._list_files(output_dir)
            error_info = f" Errors: {processing_errors}" if processing_errors else ""
            detail = f"No MusicXML output found (job_id={job_id}, files={files}, log={book_log}).{error_info}"
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
