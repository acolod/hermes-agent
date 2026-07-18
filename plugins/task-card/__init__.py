"""Persisted, topic-bound live task cards for Hermes gateway conversations."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_cli.plugins import PluginCommandContext
from hermes_constants import get_hermes_home


logger = logging.getLogger(__name__)

TERMINAL_PHASES = {
    "blocked",
    "cancelled",
    "cancelled_by_user",
    "completed",
    "failed",
    "ready_for_alex",
}
START_PHASES = {"background-start", "foreground-start"}
PHASE_RANK = {
    "queued": 0,
    "command": 1,
    "background-start": 2,
    "foreground-start": 2,
    "running": 3,
    "working": 3,
    "phase": 4,
    "in_progress": 4,
    "drafting": 5,
    **{phase: 100 for phase in TERMINAL_PHASES},
}
SHOW_COMMANDS = {"", "show", "status", "view", "render"}
TERMINAL_COMMANDS = {"close", "done", "finish", "terminal"}
MAX_LABEL_LENGTH = 128
MAX_SUMMARY_LENGTH = 256


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def binding_from_origin(
    *,
    platform: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    session_key: str | None = None,
    surface: str | None = None,
) -> str:
    parts = [platform, chat_id, thread_id, session_key]
    cleaned = [str(part).strip() for part in parts if str(part or "").strip()]
    return ":".join(cleaned) if cleaned else f"{surface or 'taskcard'}:unbound"


def _topic_identity(context: PluginCommandContext) -> str:
    return binding_from_origin(
        platform=context.origin.platform,
        chat_id=context.origin.chat_id,
        thread_id=context.origin.thread_id,
        session_key=context.origin.session_key,
        surface=str(context.metadata.get("surface") or context.command),
    )


def _phase_rank(phase: str) -> int:
    return PHASE_RANK.get((phase or "").strip().lower(), 10)


@dataclass(frozen=True)
class TaskCardState:
    binding: str
    topic_identity: str
    generation: str
    activity_kind: str
    activity_id: str
    platform_message_id: str | None
    revision: int
    revision_hash: str
    content_hash: str
    command: str
    phase: str
    status: str
    terminal: bool
    surface: str
    platform: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    session_key: str | None = None
    summary: str = ""
    updated_at: str = ""
    activity_snapshot: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _clean(asdict(self))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskCardState":
        return cls(
            binding=str(payload.get("binding") or ""),
            topic_identity=str(payload.get("topic_identity") or ""),
            generation=str(
                payload.get("generation")
                or payload.get("revision_hash")
                or "legacy"
            ),
            activity_kind=str(payload.get("activity_kind") or ""),
            activity_id=str(payload.get("activity_id") or ""),
            platform_message_id=(
                str(payload["platform_message_id"])
                if payload.get("platform_message_id") is not None
                else None
            ),
            revision=int(payload.get("revision") or 0),
            revision_hash=str(payload.get("revision_hash") or ""),
            content_hash=str(payload.get("content_hash") or ""),
            command=str(payload.get("command") or "taskcard"),
            phase=str(payload.get("phase") or ""),
            status=str(payload.get("status") or ""),
            terminal=bool(payload.get("terminal", False)),
            surface=str(payload.get("surface") or "gateway"),
            platform=payload.get("platform") or None,
            chat_id=payload.get("chat_id") or None,
            thread_id=payload.get("thread_id") or None,
            session_key=payload.get("session_key") or None,
            summary=str(payload.get("summary") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            activity_snapshot=dict(payload.get("activity_snapshot") or {}),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class TaskCardEvent:
    binding: str
    topic_identity: str
    generation: str
    activity_kind: str
    activity_id: str
    command: str
    phase: str
    status: str
    terminal: bool
    surface: str
    platform: str | None
    chat_id: str | None
    thread_id: str | None
    session_key: str | None
    summary: str
    activity_snapshot: dict[str, Any]
    metadata: dict[str, Any]

    def revision_hash(self) -> str:
        return _stable_hash(_clean(asdict(self)))


def render_task_card(state: TaskCardState) -> str:
    lines = [
        "╭─ Task Card ─────────────────────────────╮",
        f"│ binding   : {state.binding}",
        f"│ revision  : rev {state.revision}  hash {state.revision_hash[:12]}",
        f"│ status    : {state.status}{' (terminal)' if state.terminal else ''}",
        f"│ phase     : {state.phase}",
        f"│ command   : /{state.command}",
    ]
    route = [state.platform, state.chat_id, state.thread_id, state.session_key]
    route = [str(part) for part in route if part]
    if route:
        lines.append(f"│ route     : {' · '.join(route)}")
    if state.summary:
        lines.append(f"│ summary   : {state.summary}")
    if state.activity_id:
        lines.append(f"│ activity  : {state.activity_id}")
    lines.append("╰──────────────────────────────────────────╯")
    return "\n".join(lines)


def _with_content_hash(state: TaskCardState) -> TaskCardState:
    digest = hashlib.sha256(render_task_card(state).encode("utf-8")).hexdigest()
    return replace(state, content_hash=digest)


def reduce_task_card_state(
    previous: TaskCardState | None,
    event: TaskCardEvent,
) -> TaskCardState:
    event_hash = event.revision_hash()
    if previous is not None:
        same_generation = previous.generation == event.generation
        if same_generation:
            if previous.terminal or previous.revision_hash == event_hash:
                return previous
            if (
                previous.activity_id
                and event.activity_id
                and previous.activity_id != event.activity_id
            ):
                return previous
            if _phase_rank(event.phase) < _phase_rank(previous.phase) and not event.terminal:
                return previous
            revision = previous.revision + 1
        else:
            revision = 1
    else:
        revision = 1

    state = TaskCardState(
        binding=event.binding,
        topic_identity=event.topic_identity,
        generation=event.generation,
        activity_kind=event.activity_kind,
        activity_id=event.activity_id,
        platform_message_id=(
            previous.platform_message_id if previous is not None else None
        ),
        revision=revision,
        revision_hash=event_hash,
        content_hash="",
        command=event.command,
        phase=event.phase,
        status=event.status,
        terminal=event.terminal,
        surface=event.surface,
        platform=event.platform,
        chat_id=event.chat_id,
        thread_id=event.thread_id,
        session_key=event.session_key,
        summary=event.summary,
        updated_at=now_iso(),
        activity_snapshot=event.activity_snapshot,
        metadata=event.metadata,
    )
    return _with_content_hash(state)


class TaskCardStore:
    """Atomic JSON persistence for cards and topic-to-binding identity."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else get_hermes_home() / "plugins" / "task-card"
        self.cards_dir = self.root / "cards"
        self.bindings_dir = self.root / "bindings"
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        self.bindings_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def slug_for_card(topic_identity: str, binding: str) -> str:
        identity = f"{topic_identity}\0{binding}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def path_for_card(self, topic_identity: str, binding: str) -> Path:
        return self.cards_dir / f"{self.slug_for_card(topic_identity, binding)}.json"

    def path_for_binding(self, topic_identity: str) -> Path:
        digest = hashlib.sha256(topic_identity.encode("utf-8")).hexdigest()[:24]
        return self.bindings_dir / f"{digest}.json"

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _load_path(self, path: Path) -> TaskCardState | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            state = TaskCardState.from_dict(payload)
        except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not state.content_hash:
            state = _with_content_hash(state)
        return state

    def load(
        self,
        binding: str,
        topic_identity: str | None = None,
    ) -> TaskCardState | None:
        if topic_identity is not None:
            state = self._load_path(self.path_for_card(topic_identity, binding))
            if (
                state is None
                or state.binding != binding
                or state.topic_identity != topic_identity
            ):
                return None
            return state
        matches = [
            state
            for path in self.cards_dir.glob("*.json")
            if (state := self._load_path(path)) is not None and state.binding == binding
        ]
        return matches[0] if len(matches) == 1 else None

    def save(self, state: TaskCardState) -> TaskCardState:
        with self._lock:
            self._atomic_write(
                self.path_for_card(state.topic_identity, state.binding),
                state.to_dict(),
            )
        return state

    def _load_binding(self, topic_identity: str) -> str | None:
        try:
            payload = json.loads(
                self.path_for_binding(topic_identity).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("topic_identity") or "") != topic_identity:
            return None
        binding = str(payload.get("binding") or "").strip()
        return binding or None

    def bind_topic(self, topic_identity: str, binding: str) -> None:
        with self._lock:
            self._atomic_write(
                self.path_for_binding(topic_identity),
                {
                    "version": 1,
                    "topic_identity": topic_identity,
                    "binding": binding,
                },
            )

    def resolve_binding(self, topic_identity: str) -> str | None:
        with self._lock:
            return self._load_binding(topic_identity)

    def clear(self, binding: str, topic_identity: str) -> None:
        with self._lock:
            try:
                self.path_for_card(topic_identity, binding).unlink()
            except FileNotFoundError:
                pass
            if self._load_binding(topic_identity) == binding:
                try:
                    self.path_for_binding(topic_identity).unlink()
                except FileNotFoundError:
                    pass

    def list_bindings(self) -> list[str]:
        bindings: list[str] = []
        for path in sorted(self.cards_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            binding = str(payload.get("binding") or "") if isinstance(payload, dict) else ""
            if binding:
                bindings.append(binding)
        return bindings


class TaskCardManager:
    """Reduce lifecycle events, persist cards, and publish on the owning loop."""

    def __init__(
        self,
        store: TaskCardStore | None = None,
        *,
        debounce_seconds: float = 0.75,
        publish_retry_seconds: float = 0.25,
        publish_retry_attempts: int = 2,
    ):
        self.store = store or TaskCardStore()
        self.debounce_seconds = max(0.0, float(debounce_seconds))
        self.publish_retry_seconds = max(0.0, float(publish_retry_seconds))
        self.publish_retry_attempts = max(1, int(publish_retry_attempts))
        self._state_cache: dict[tuple[str, str], TaskCardState] = {}
        self._publishers: dict[tuple[str, str], Any] = {}
        self._pending_handles: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._publish_tasks: set[asyncio.Task[Any]] = set()
        self._lock = threading.RLock()

    def current_state(
        self,
        binding: str,
        topic_identity: str | None = None,
    ) -> TaskCardState | None:
        if topic_identity is None:
            matches = [
                state
                for (_, label), state in self._state_cache.items()
                if label == binding
            ]
            if len(matches) == 1:
                return matches[0]
            state = self.store.load(binding)
            if state is not None:
                self._state_cache[(state.topic_identity, binding)] = state
            return state
        key = (topic_identity, binding)
        state = self._state_cache.get(key)
        if state is None:
            state = self.store.load(binding, topic_identity)
            if state is not None:
                self._state_cache[key] = state
        return state

    def _cancel_pending(self, key: tuple[str, str]) -> None:
        handle = self._pending_handles.pop(key, None)
        if handle is not None:
            handle.cancel()

    async def _publish(self, key: tuple[str, str], publisher: Any) -> None:
        topic_identity, binding = key
        state = self.current_state(binding, topic_identity)
        if state is None:
            return
        metadata = {
            "binding": state.binding,
            "generation": state.generation,
            "revision_hash": state.revision_hash,
            "content_hash": state.content_hash,
            "phase": state.phase,
            "status": state.status,
            "terminal": state.terminal,
            "topic_identity": state.topic_identity,
            "validate_edit_response": True,
        }
        if state.platform_message_id:
            metadata["status_message_id"] = state.platform_message_id

        logger.info(
            "task-card publish begin binding=%s generation=%s revision=%s phase=%s "
            "activity=%s message_id=%s",
            state.binding,
            state.generation,
            state.revision,
            state.phase,
            state.activity_id,
            state.platform_message_id,
        )

        for attempt in range(1, self.publish_retry_attempts + 1):
            current = self.current_state(binding, topic_identity)
            if (
                current is None
                or current.generation != state.generation
                or current.revision != state.revision
            ):
                return
            try:
                result = publisher.upsert_status(
                    "taskcard",
                    render_task_card(state),
                    revision=state.revision,
                    metadata=metadata,
                )
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                logger.warning(
                    "task-card status publish attempt %s/%s failed: %s",
                    attempt,
                    self.publish_retry_attempts,
                    exc,
                )
                result = None
                success = False
            else:
                success = result is None or bool(getattr(result, "success", True))

            raw_response = getattr(result, "raw_response", None) if result is not None else None
            disposition = None
            if isinstance(raw_response, dict):
                disposition = {
                    key: str(raw_response[key])[:64]
                    for key in ("status", "reason", "error_kind")
                    if raw_response.get(key) is not None
                }
            logger.info(
                "task-card publish result binding=%s generation=%s revision=%s "
                "success=%s message_id=%s disposition=%s",
                state.binding,
                state.generation,
                state.revision,
                success,
                getattr(result, "message_id", None) if result is not None else None,
                disposition,
            )

            if success:
                message_id = getattr(result, "message_id", None) if result is not None else None
                if message_id:
                    with self._lock:
                        current = self.current_state(binding, topic_identity)
                        if (
                            current is not None
                            and current.generation == state.generation
                            and current.revision == state.revision
                        ):
                            current = replace(
                                current,
                                platform_message_id=str(message_id),
                            )
                            self._state_cache[key] = current
                            self.store.save(current)
                return
            if attempt < self.publish_retry_attempts:
                await asyncio.sleep(self.publish_retry_seconds)

        logger.warning(
            "task-card status publish exhausted %s attempts for %s",
            self.publish_retry_attempts,
            binding,
        )

    def _spawn_publish(self, key: tuple[str, str], publisher: Any) -> None:
        task = asyncio.get_running_loop().create_task(self._publish(key, publisher))
        self._publish_tasks.add(task)
        task.add_done_callback(self._publish_tasks.discard)

    def _schedule_publish(self, state: TaskCardState, publisher: Any) -> None:
        key = (state.topic_identity, state.binding)
        self._cancel_pending(key)
        if publisher is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if state.terminal or self.debounce_seconds == 0:
            self._spawn_publish(key, publisher)
            return

        def enqueue() -> None:
            self._pending_handles.pop(key, None)
            self._spawn_publish(key, publisher)

        self._pending_handles[key] = loop.call_later(
            self.debounce_seconds,
            enqueue,
        )

    async def wait_for_publishes(self) -> None:
        while any(not handle.cancelled() for handle in self._pending_handles.values()):
            await asyncio.sleep(self.debounce_seconds + 0.01)
        while self._publish_tasks:
            await asyncio.gather(*list(self._publish_tasks))

    def _record(self, event: TaskCardEvent, publisher: Any) -> TaskCardState:
        with self._lock:
            key = (event.topic_identity, event.binding)
            previous = self.current_state(event.binding, event.topic_identity)
            next_state = reduce_task_card_state(previous, event)
            if next_state is not previous:
                self._state_cache[key] = next_state
                self.store.save(next_state)
            if publisher is not None:
                self._publishers[key] = publisher
            self._schedule_publish(
                next_state,
                publisher or self._publishers.get(key),
            )
            return next_state

    def _event(
        self,
        context: PluginCommandContext,
        *,
        binding: str,
        phase: str,
        status: str,
        summary: str,
        activity_snapshot: dict[str, Any] | None = None,
        terminal: bool = False,
        generation: str | None = None,
        activity_kind: str | None = None,
        activity_id: str | None = None,
    ) -> TaskCardEvent:
        topic_identity = _topic_identity(context)
        previous = self.current_state(binding, topic_identity)
        resolved_generation = (
            generation
            or (previous.generation if previous is not None else uuid.uuid4().hex)
        )
        preserve_activity = bool(
            previous is not None and previous.generation == resolved_generation
        )
        return TaskCardEvent(
            binding=binding,
            topic_identity=topic_identity,
            generation=resolved_generation,
            activity_kind=(
                str(activity_kind or "")
                or (previous.activity_kind if preserve_activity and previous else "")
            ),
            activity_id=(
                str(activity_id or "")
                or (previous.activity_id if preserve_activity and previous else "")
            ),
            command=context.command,
            phase=phase,
            status=status,
            terminal=terminal,
            surface=str(context.metadata.get("surface") or context.origin.platform or "gateway"),
            platform=context.origin.platform,
            chat_id=context.origin.chat_id,
            thread_id=context.origin.thread_id,
            session_key=context.origin.session_key,
            summary=str(summary)[:MAX_SUMMARY_LENGTH],
            activity_snapshot=_clean(activity_snapshot or {}),
            metadata=_clean(dict(context.metadata or {})),
        )

    def handle_command(self, raw_args: str, context: PluginCommandContext) -> str:
        normalized = (raw_args or "").strip()
        command, _, remainder = normalized.partition(" ")
        command = command.lower()
        topic_identity = _topic_identity(context)
        binding = self.store.resolve_binding(topic_identity)

        if command in SHOW_COMMANDS:
            if binding is None:
                return "Task card is not bound yet. Use /taskcard bind <label> in this conversation."
            state = self.current_state(binding, topic_identity)
            if state is None:
                return "Task card is not bound yet. Use /taskcard bind <label> in this conversation."
            return render_task_card(state)

        if command == "bind":
            requested_label = remainder.strip()
            if len(requested_label) > MAX_LABEL_LENGTH:
                return f"Task card label must be {MAX_LABEL_LENGTH} characters or fewer."
            label = requested_label or f"task-{hashlib.sha256(topic_identity.encode()).hexdigest()[:12]}"
            previous_binding = self.store.resolve_binding(topic_identity)
            if previous_binding is not None:
                previous_key = (topic_identity, previous_binding)
                self._cancel_pending(previous_key)
                self.store.clear(previous_binding, topic_identity)
                self._state_cache.pop(previous_key, None)
                self._publishers.pop(previous_key, None)
            self.store.bind_topic(topic_identity, label)
            event = self._event(
                context,
                binding=label,
                phase="command",
                status="bound",
                summary=f"Bound to {label}",
                generation=uuid.uuid4().hex,
            )
            state = self._record(event, context.status)
            if context.status is not None:
                return f"Task card bound: {label}"
            return render_task_card(state)

        if binding is None:
            return "Task card is not bound yet. Use /taskcard bind <label> in this conversation."

        key = (topic_identity, binding)

        if command == "reset":
            self._cancel_pending(key)
            self.store.clear(binding, topic_identity)
            self._state_cache.pop(key, None)
            self._publishers.pop(key, None)
            return f"Task card binding cleared: {binding}"

        if command == "flush":
            state = self.current_state(binding, topic_identity)
            if state is None:
                return "Task card has no state to flush yet."
            publisher = context.status or self._publishers.get(key)
            if publisher is not None:
                self._spawn_publish(key, publisher)
                return f"Task card flush queued: {binding}"
            return render_task_card(state)

        terminal = command in TERMINAL_COMMANDS
        event = self._event(
            context,
            binding=binding,
            phase="completed" if terminal else "command",
            status="completed" if terminal else "running",
            summary=(remainder.strip() or command or "Task card update")[:MAX_SUMMARY_LENGTH],
            terminal=terminal,
        )
        state = self._record(event, context.status)
        if context.status is not None:
            return f"Task card updated: {binding}"
        return render_task_card(state)

    def on_gateway_activity(
        self,
        *,
        context: PluginCommandContext,
        activity_snapshot: dict[str, Any] | None = None,
        terminal: bool | None = None,
    ) -> TaskCardState | None:
        topic_identity = _topic_identity(context)
        binding = self.store.resolve_binding(topic_identity)
        if binding is None:
            return None
        activity = dict(activity_snapshot or {})
        activity_kind = str(activity.get("kind") or "foreground")
        task_id = str(activity.get("task_id") or "")
        activity_id = (
            f"{activity_kind}:{task_id}"
            if task_id
            else activity_kind
        )
        phase = str(activity.get("phase") or "running")
        status = str(activity.get("status") or phase)
        is_terminal = (
            bool(activity.get("terminal"))
            if terminal is None
            else bool(terminal)
        )
        is_terminal = is_terminal or phase in TERMINAL_PHASES
        previous = self.current_state(binding, topic_identity)
        generation = (
            uuid.uuid4().hex
            if phase in START_PHASES and previous is not None and previous.terminal
            else None
        )
        logger.info(
            "task-card activity binding=%s kind=%s phase=%s activity=%s "
            "previous_generation=%s next_generation=%s publisher=%s message_id=%s",
            binding,
            activity_kind,
            phase,
            activity_id,
            previous.generation if previous is not None else None,
            generation or (previous.generation if previous is not None else None),
            context.status is not None,
            previous.platform_message_id if previous is not None else None,
        )
        event = self._event(
            context,
            binding=binding,
            phase=phase,
            status=status,
            summary=str(activity.get("summary") or phase),
            activity_snapshot=activity,
            terminal=is_terminal,
            generation=generation,
            activity_kind=activity_kind,
            activity_id=activity_id,
        )
        return self._record(event, context.status)


_MANAGERS: dict[str, TaskCardManager] = {}


def get_manager(context: PluginCommandContext | None = None) -> TaskCardManager:
    profile = context.origin.profile if context is not None else None
    if profile:
        from hermes_cli.profiles import get_profile_dir

        profile_home = Path(get_profile_dir(profile))
    else:
        profile_home = Path(get_hermes_home())
    root = profile_home / "plugins" / "task-card"
    key = str(root.resolve())
    manager = _MANAGERS.get(key)
    if manager is None:
        manager = TaskCardManager(TaskCardStore(root))
        _MANAGERS[key] = manager
    return manager


def _handle_taskcard(
    raw_args: str,
    context: PluginCommandContext | None = None,
) -> str:
    if context is None:
        return "Task card command needs a plugin context."
    return get_manager(context).handle_command(raw_args, context)


def _on_gateway_activity(
    *,
    context: PluginCommandContext,
    activity_snapshot: dict[str, Any] | None = None,
    terminal: bool | None = None,
    **_: Any,
) -> TaskCardState | None:
    return get_manager(context).on_gateway_activity(
        context=context,
        activity_snapshot=activity_snapshot,
        terminal=terminal,
    )


def register(ctx: Any) -> None:
    ctx.register_command(
        "taskcard",
        _handle_taskcard,
        description="Bind and render the live task card for this conversation",
        args_hint="[show|bind <label>|flush|reset|close]",
    )
    ctx.register_hook("gateway_activity", _on_gateway_activity)


__all__ = [
    "TaskCardEvent",
    "TaskCardManager",
    "TaskCardState",
    "TaskCardStore",
    "binding_from_origin",
    "get_manager",
    "reduce_task_card_state",
    "register",
    "render_task_card",
]
