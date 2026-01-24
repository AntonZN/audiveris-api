import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from api.config import settings


def _cleanup_root(root: Path, cutoff_ts: float) -> None:
    if not root.exists():
        return
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime < cutoff_ts:
            shutil.rmtree(child, ignore_errors=True)


def cleanup_storage() -> None:
    """Remove stale task directories from input/output roots."""
    if settings.task_ttl_seconds <= 0:
        return
    cutoff_ts = datetime.now(timezone.utc).timestamp() - settings.task_ttl_seconds
    _cleanup_root(Path(settings.input_dir), cutoff_ts)
    _cleanup_root(Path(settings.output_dir), cutoff_ts)


def start_cleanup_loop(stop_event: threading.Event) -> threading.Thread:
    """Run periodic cleanup in a background thread."""
    def _loop() -> None:
        cleanup_storage()
        while not stop_event.wait(settings.cleanup_interval_seconds):
            cleanup_storage()

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return thread
