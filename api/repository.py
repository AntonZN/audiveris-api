import json
from datetime import datetime, timezone
from typing import Any

import redis

from api.config import settings


class TaskRepository:
    def __init__(self) -> None:
        self._redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)

    def _task_key(self, task_id: str) -> str:
        return f"{settings.task_key_prefix}{task_id}"

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def save(self, task: dict[str, Any]) -> None:
        task["updated_at"] = self._now()
        self._redis.set(self._task_key(task["id"]), json.dumps(task, sort_keys=True, default=str))

    def get(self, task_id: str) -> dict[str, Any] | None:
        payload = self._redis.get(self._task_key(task_id))
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    def update(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        task = self.get(task_id)
        if not task:
            return None
        task.update(fields)
        self.save(task)
        return task

    def enqueue(self, task_id: str) -> None:
        self._redis.rpush(settings.task_queue_key, task_id)

    def dequeue(self, timeout: int = 0) -> str | None:
        item = self._redis.blpop(settings.task_queue_key, timeout=timeout)
        if not item:
            return None
        _, task_id = item
        return task_id

    def queue_depth(self) -> int:
        return self._redis.llen(settings.task_queue_key)

    def requeue_running_tasks(self) -> None:
        if not settings.requeue_running:
            return
        cursor = 0
        pattern = f"{settings.task_key_prefix}*"
        while True:
            cursor, keys = self._redis.scan(cursor=cursor, match=pattern, count=200)
            for key in keys:
                payload = self._redis.get(key)
                if not payload:
                    continue
                try:
                    task = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if task.get("status") == "running":
                    task["status"] = "queued"
                    task_id = task.get("id", key[len(settings.task_key_prefix):])
                    self.save(task)
                    self._redis.rpush(settings.task_queue_key, task_id)
            if cursor == 0:
                break


repo = TaskRepository()
