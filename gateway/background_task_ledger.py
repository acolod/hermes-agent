"""Minimal durable ledger for background tasks that outlive a gateway process."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from utils import atomic_json_write

_VERSION = 1
logger = logging.getLogger(__name__)


class BackgroundTaskLedger:
    """Persist only active ``bg_*`` task metadata; terminal records are removed."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "background-tasks.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("version") != _VERSION:
            return {}
        records = payload.get("active")
        if not isinstance(records, dict):
            return {}
        return {
            task_id: dict(record)
            for task_id, record in records.items()
            if isinstance(task_id, str) and task_id.startswith(("bg_", "bg-")) and isinstance(record, dict)
        }

    def _save(self, records: dict[str, dict[str, Any]]) -> None:
        atomic_json_write(self.path, {"version": _VERSION, "active": records})

    def _save_safely(self, records: dict[str, dict[str, Any]]) -> bool:
        try:
            self._save(records)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Could not persist background task ledger %s: %s", self.path, exc)
            return False
        return True

    def active_records(self) -> dict[str, dict[str, Any]]:
        """Return a copy of valid active records without consuming them."""
        return {task_id: dict(record) for task_id, record in self._load().items()}

    def register(self, task_id: str, record: dict[str, Any]) -> bool:
        if not task_id.startswith(("bg_", "bg-")):
            raise ValueError("background task id must start with bg_ or bg-")
        records = self._load()
        records[task_id] = dict(record)
        return self._save_safely(records)

    def update_todos(self, task_id: str, task_items: list[dict[str, Any]]) -> bool:
        records = self._load()
        if task_id not in records:
            return False
        records[task_id]["task_items"] = list(task_items)
        return self._save_safely(records)

    def update_metadata(self, task_id: str, **metadata: Any) -> bool:
        records = self._load()
        if task_id not in records:
            return False
        records[task_id].update(metadata)
        return self._save_safely(records)

    def pop_all(self) -> dict[str, dict[str, Any]]:
        records = self._load()
        self._save_safely({})
        return records

    def clear(self, task_id: str) -> bool:
        records = self._load()
        if task_id in records:
            records.pop(task_id)
            return self._save_safely(records)
        return False
