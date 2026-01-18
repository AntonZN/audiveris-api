import threading
from pathlib import Path

from api.models import TaskStatus
from api.repository import repo
from api.services import audiveris_service


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
                self._process_task(task_id)

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
        fail_on_error = task.get("fail_on_error", True)

        input_paths = [input_dir / fname for fname in input_files]
        results: list[dict] = []
        errors: list[str] = []
        completed_count = 0
        failed_count = 0

        if playlist and len(input_paths) > 0:
            # Process all files as a single playlist (one book -> one MusicXML)
            result = audiveris_service.process_playlist(input_paths, output_dir)
            results.append(result.model_dump())
            if result.error:
                errors.append(result.error)
                failed_count = 1
            else:
                completed_count = 1
        else:
            # Process each file individually
            total = len(input_paths)
            for i, input_path in enumerate(input_paths):
                result = audiveris_service.process_single(input_path, output_dir)
                results.append(result.model_dump())

                if result.error:
                    errors.append(result.error)
                    failed_count += 1
                    if fail_on_error:
                        # Stop processing on first error
                        break
                else:
                    completed_count += 1

                # Update progress
                task["progress"] = {
                    "total": total,
                    "completed": completed_count,
                    "failed": failed_count,
                }
                task["results"] = results
                task["errors"] = errors
                repo.save(task)

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
        elif completed_count > 0 and not fail_on_error:
            task["status"] = TaskStatus.partial.value
        else:
            task["status"] = TaskStatus.error.value

        repo.save(task)


def create_workers(count: int) -> list[Worker]:
    """Create and start multiple workers."""
    workers = []
    for _ in range(max(count, 1)):
        worker = Worker()
        worker.start()
        workers.append(worker)
    return workers
