import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from api.config import VALIDATE_DIR_PREFIX, settings


def _cleanup_root(
    root: Path,
    cutoff_ts: float,
    name_prefix: str | None = None,
    exclude: set[Path] | None = None,
) -> None:
    if not root.exists():
        return
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if exclude and child.resolve() in exclude:
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

    # Медиа каталога живут в catalog_media_dir (обычно /storage/out/catalog,
    # т.е. прямым потомком output_dir) и НЕ являются временными выходами OMR —
    # их нельзя удалять по TTL задач, иначе чистка снесёт весь каталог.
    # failures_dir (архив проваленных входов для аудита) тоже прямой потомок
    # output_dir и живёт постоянно — чистит его только админ вручную.
    protected = {
        Path(settings.catalog_media_dir).resolve(),
        Path(settings.failures_dir).resolve(),
    }

    if settings.task_ttl_seconds > 0:
        cutoff_ts = now - settings.task_ttl_seconds
        _cleanup_root(Path(settings.input_dir), cutoff_ts, exclude=protected)
        _cleanup_root(output_dir, cutoff_ts, exclude=protected)

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
