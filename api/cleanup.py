import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from api.config import VALIDATE_DIR_PREFIX, settings


def _cleanup_root(root: Path, cutoff_ts: float, name_prefix: str | None = None) -> None:
    if not root.exists():
        return
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if name_prefix is not None and not child.name.startswith(name_prefix):
            continue
        try:
            mtime = child.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime < cutoff_ts:
            shutil.rmtree(child, ignore_errors=True)


def cleanup_storage() -> None:
    """Remove stale task and /validate directories from input/output roots."""
    now = datetime.now(timezone.utc).timestamp()
    output_dir = Path(settings.output_dir)

    if settings.task_ttl_seconds > 0:
        cutoff_ts = now - settings.task_ttl_seconds
        _cleanup_root(Path(settings.input_dir), cutoff_ts)
        _cleanup_root(output_dir, cutoff_ts)

    # /validate выходы лежат в output_dir с префиксом VALIDATE_DIR_PREFIX и имеют
    # свой, более короткий TTL.
    if settings.validate_ttl_seconds > 0:
        _cleanup_root(
            output_dir,
            now - settings.validate_ttl_seconds,
            name_prefix=VALIDATE_DIR_PREFIX,
        )


def start_cleanup_loop(stop_event: threading.Event) -> threading.Thread:
    """Run periodic cleanup in a background thread."""
    def _loop() -> None:
        cleanup_storage()
        while not stop_event.wait(settings.cleanup_interval_seconds):
            cleanup_storage()

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return thread
