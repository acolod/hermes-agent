"""Private, bounded Todo snapshot persistence for automation callers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from tools.todo_tool import VALID_STATUSES

MAX_SNAPSHOT_ITEMS = 16
MAX_SNAPSHOT_ID_CHARS = 128
MAX_SNAPSHOT_CONTENT_CHARS = 160


class TodoSnapshotWriter:
    """Observe completed Todo tool results and atomically persist changed snapshots."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def observe(self, _messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> bool:
        """Persist the last valid Todo result in one completed tool batch.

        Returns True only when a changed valid snapshot was written. Invalid
        results are intentionally ignored so observing never disrupts the turn.
        """
        snapshot: list[dict[str, str]] | None = None
        for tool in tools or []:
            if not isinstance(tool, dict) or tool.get("name") != "todo":
                continue
            candidate = self._normalize_result(tool.get("result"))
            if candidate is not None:
                snapshot = candidate
        if snapshot is None:
            return False
        try:
            if self._read_existing() == snapshot:
                return False
            self._write_atomic(snapshot)
        except OSError:
            return False
        return True

    @staticmethod
    def _normalize_result(result: Any) -> list[dict[str, str]] | None:
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                return None
        if not isinstance(result, dict) or not isinstance(result.get("todos"), list):
            return None

        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in result["todos"]:
            if not isinstance(raw, dict):
                continue
            item_id = str(raw.get("id") or "").strip()[:MAX_SNAPSHOT_ID_CHARS]
            content = str(raw.get("content") or "").strip()[:MAX_SNAPSHOT_CONTENT_CHARS]
            status = str(raw.get("status") or "").strip().lower()
            if not item_id or not content or status not in VALID_STATUSES or item_id in seen:
                continue
            normalized.append({"id": item_id, "content": content, "status": status})
            seen.add(item_id)
            if len(normalized) >= MAX_SNAPSHOT_ITEMS:
                break
        return normalized or None

    def _read_existing(self) -> list[dict[str, str]] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, list) else None

    def _write_atomic(self, snapshot: list[dict[str, str]]) -> None:
        self._ensure_private_parent()
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, separators=(",", ":"), ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _ensure_private_parent(self) -> None:
        """Reject symlink traversal, then create a private direct parent."""
        target = self.path.absolute()
        for candidate in (target, *target.parents):
            try:
                if candidate.is_symlink():
                    raise OSError("refusing symlinked Todo snapshot path")
            except OSError:
                raise

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink():
            raise OSError("refusing symlinked Todo snapshot parent")
        os.chmod(self.path.parent, 0o700)
