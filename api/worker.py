import shutil
import threading
import traceback
from pathlib import Path

from api.failures import record_failure
from api.models import TaskStatus
from api.repository import repo
from api.services import audiveris_service
from api.stats import record_processing


class Worker:
    def __init__(self) -> None:
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the worker in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the worker to stop."""
        self._running = False

    def _run(self) -> None:
        """Main worker loop."""
        while self._running:
            task_id = repo.dequeue(timeout=1)
            if task_id:
                try:
                    self._process_task(task_id)
                except Exception:
                    task = repo.get(task_id)
                    if task:
                        error_text = traceback.format_exc()
                        task["status"] = TaskStatus.error.value
                        task["errors"] = error_text
                        progress = task.get("progress") or {}
                        total = progress.get("total", 1)
                        completed = progress.get("completed", 0)
                        task["progress"] = {
                            "total": total,
                            "completed": completed,
                            "failed": max(1, progress.get("failed", 0)),
                        }
                        repo.save(task)
                        record_processing(
                            task_id=task_id,
                            kind="playlist" if task.get("playlist") else "single",
                            preset=task.get("preset"),
                            status=TaskStatus.error.value,
                            files_total=total,
                            files_completed=completed,
                            files_failed=max(1, progress.get("failed", 0)),
                            enhance=bool(task.get("enhance")),
                            analyze=bool(task.get("analyze")),
                        )
                        # Неожиданный сбой воркера: тоже архивируем входы для аудита,
                        # прежде чем input_dir будет удалён (в т.ч. cleanup'ом по TTL).
                        self._archive_failure(task, error_text)

    def _process_task(self, task_id: str) -> None:
        """Process a single task from the queue."""
        task = repo.get(task_id)
        if not task or task.get("status") not in {"queued", "running"}:
            return

        task["status"] = TaskStatus.running.value
        repo.save(task)

        input_files = task.get("input_files", [])
        input_dir = Path(task.get("input_dir", ""))
        output_dir = Path(task.get("output_dir", ""))
        playlist = task.get("playlist", False)
        preset = task.get("preset", "default")
        enhance = task.get("enhance", False)

        input_paths = [input_dir / fname for fname in input_files]
        errors = None
        completed_count = 0
        failed_count = 0

        if playlist and len(input_paths) > 0:
            # Process all files as a single playlist (one book -> one MusicXML)
            res = audiveris_service.process_playlist(
                input_paths, output_dir, preset, enhance=enhance,
            )
            results = res.model_dump()

            if res.error:
                errors = res.error
                failed_count = 1
            else:
                completed_count = 1
        else:
            # Process single file
            input_path = input_paths[0]
            res = audiveris_service.process_single(
                input_path, output_dir, preset, enhance=enhance,
            )
            results = res.model_dump()

            if res.error:
                errors = res.error
                failed_count = 1
            else:
                completed_count = 1

        # Determine final status
        task["results"] = results
        task["errors"] = errors
        task["progress"] = {
            "total": len(input_paths) if not playlist else 1,
            "completed": completed_count,
            "failed": failed_count,
        }

        if failed_count == 0:
            task["status"] = TaskStatus.completed.value
        else:
            task["status"] = TaskStatus.error.value

        repo.save(task)

        record_processing(
            task_id=task_id,
            kind="playlist" if playlist else "single",
            preset=preset,
            status=task["status"],
            files_total=task["progress"]["total"],
            files_completed=completed_count,
            files_failed=failed_count,
            enhance=bool(enhance),
            analyze=bool(task.get("analyze")),
        )

        # Провал: не удаляем входы молча — копируем их в архив провалов и заводим
        # строки в failed_files для последующего аудита (см. api/failures.py).
        if failed_count > 0:
            record_failure(
                task_id=task_id,
                kind="playlist" if playlist else "single",
                preset=preset,
                enhance=bool(enhance),
                input_paths=input_paths,
                error=errors,
            )

        shutil.rmtree(input_dir, ignore_errors=True)

    def _archive_failure(self, task: dict, error: str) -> None:
        """Заархивировать входы задачи при неожиданном сбое воркера (см. _run)."""
        input_dir = Path(task.get("input_dir", ""))
        input_files = task.get("input_files", [])
        input_paths = [input_dir / fname for fname in input_files]
        record_failure(
            task_id=task.get("id") or task.get("task_id"),
            kind="playlist" if task.get("playlist") else "single",
            preset=task.get("preset"),
            enhance=bool(task.get("enhance")),
            input_paths=input_paths,
            error=error,
        )


def create_workers(count: int) -> list[Worker]:
    """Create and start multiple workers."""
    workers = []
    for _ in range(max(count, 1)):
        worker = Worker()
        worker.start()
        workers.append(worker)
    return workers
