"""Focused tests for the installable task-card plugin."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_cli.plugins import (
    PluginCommandContext,
    PluginCommandOrigin,
    PluginManager,
    VALID_HOOKS,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "task-card"


def _load_plugin():
    name = f"task_card_plugin_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _context(
    *,
    status: Any = None,
    thread_id: str = "topic-9",
    profile: str | None = None,
) -> PluginCommandContext:
    return PluginCommandContext(
        command="taskcard",
        raw_args="",
        origin=PluginCommandOrigin(
            platform="telegram",
            chat_id="chat-1",
            thread_id=thread_id,
            session_key="session-1",
            profile=profile,
        ),
        status=status,
        metadata={"surface": "gateway"},
    )


class _CaptureStatus:
    def __init__(self, outcomes: list[bool] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outcomes = list(outcomes or [])

    async def upsert_status(
        self,
        status_key: str,
        content: str,
        *,
        revision: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append(
            {
                "status_key": status_key,
                "content": content,
                "revision": revision,
                "metadata": metadata,
                "loop": asyncio.get_running_loop(),
                "thread": threading.get_ident(),
            }
        )
        success = self.outcomes.pop(0) if self.outcomes else True
        return SimpleNamespace(
            success=success,
            message_id=f"message-{len(self.calls)}" if success else None,
        )


def test_manifest_is_discoverable_and_registers_contextual_command_and_hook():
    manager = PluginManager()
    manifests = manager._scan_directory(REPO_ROOT / "plugins", source="bundled")
    manifest = next(item for item in manifests if item.name == "task-card")
    assert manifest.key == "task-card"
    assert manifest.kind == "standalone"
    assert "gateway_activity" in manifest.provides_hooks
    assert "gateway_activity" in VALID_HOOKS

    plugin = _load_plugin()
    registered: dict[str, Any] = {}

    class Context:
        def register_command(self, name, handler, **metadata):
            registered["command"] = (name, handler, metadata)

        def register_hook(self, name, handler):
            registered["hook"] = (name, handler)

    plugin.register(Context())

    name, handler, metadata = registered["command"]
    assert name == "taskcard"
    assert "context" in inspect.signature(handler).parameters
    assert metadata["args_hint"].startswith("[show|bind")
    assert registered["hook"][0] == "gateway_activity"


@pytest.mark.asyncio
async def test_bind_persists_topic_identity_revision_and_content_hash(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
    )
    context = _context(status=status)

    response = manager.handle_command("bind release-card", context)
    await manager.wait_for_publishes()

    state = manager.current_state("release-card")
    assert state is not None
    assert state.binding == "release-card"
    assert state.revision == 1
    assert state.thread_id == "topic-9"
    assert state.topic_identity == "telegram:chat-1:topic-9:session-1"
    assert manager.store.resolve_binding(state.topic_identity) == "release-card"
    assert response == "Task card bound: release-card"
    rendered = status.calls[-1]["content"]
    assert state.content_hash == hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    persisted = manager.store.load("release-card")
    assert persisted == state
    assert status.calls[-1]["revision"] == 1
    assert status.calls[-1]["metadata"]["content_hash"] == state.content_hash
    assert status.calls[-1]["metadata"]["generation"] == state.generation
    assert state.platform_message_id == "message-1"


@pytest.mark.asyncio
async def test_publish_retries_failure_and_persists_platform_message_binding(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus([False, True])
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
        publish_retry_seconds=0,
    )
    context = _context(status=status)

    manager.handle_command("bind release-card", context)
    await manager.wait_for_publishes()

    state = manager.current_state("release-card")
    assert len(status.calls) == 2
    assert state is not None and state.platform_message_id == "message-2"
    assert manager.store.load("release-card") == state

    restarted_status = _CaptureStatus()
    restarted = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
        publish_retry_seconds=0,
    )
    restarted.on_gateway_activity(
        context=_context(status=restarted_status),
        activity_snapshot={
            "kind": "foreground",
            "phase": "running",
            "status": "running",
        },
    )
    await restarted.wait_for_publishes()
    assert restarted_status.calls[-1]["metadata"]["status_message_id"] == "message-2"


@pytest.mark.asyncio
async def test_obsolete_retry_stops_after_rebind(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
        publish_retry_seconds=0,
    )
    replacement_context = _context()

    class RebindingStatus:
        calls = 0

        async def upsert_status(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                manager.handle_command("bind replacement", replacement_context)
            return SimpleNamespace(success=False, message_id=None)

    status = RebindingStatus()
    manager.handle_command("bind original", _context(status=status))
    await manager.wait_for_publishes()

    assert status.calls == 1
    assert manager.store.resolve_binding(
        "telegram:chat-1:topic-9:session-1"
    ) == "replacement"


def test_bind_rejects_oversized_label_without_persisting(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path))
    response = manager.handle_command(f"bind {'x' * 129}", _context())

    assert "128 characters or fewer" in response
    assert manager.store.list_bindings() == []


@pytest.mark.asyncio
async def test_restart_simulation_resolves_persisted_binding_and_advances_revision(tmp_path):
    plugin = _load_plugin()
    context = _context()
    first = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    first.handle_command("bind release-card", context)

    restarted = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    next_state = restarted.on_gateway_activity(
        context=context,
        activity_snapshot={"phase": "running", "status": "working", "summary": "Implementing"},
    )

    assert next_state is not None
    assert next_state.binding == "release-card"
    assert next_state.revision == 2
    assert next_state.phase == "running"
    assert next_state.thread_id == "topic-9"
    assert restarted.store.load("release-card") == next_state
    assert next_state.generation == first.current_state("release-card").generation


@pytest.mark.asyncio
async def test_reset_and_rebind_same_label_starts_new_generation(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context(status=status)

    manager.handle_command("bind release-card", context)
    await manager.wait_for_publishes()
    first = manager.current_state("release-card")
    manager.handle_command("reset", context)
    manager.handle_command("bind release-card", context)
    await manager.wait_for_publishes()
    second = manager.current_state("release-card")

    assert first is not None and second is not None
    assert first.generation != second.generation
    assert second.revision == 1
    assert status.calls[-1]["revision"] == 1


@pytest.mark.asyncio
async def test_stale_and_post_terminal_lifecycle_updates_are_ignored(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context()
    manager.handle_command("bind release-card", context)
    running = manager.on_gateway_activity(
        context=context,
        activity_snapshot={"phase": "running", "status": "working"},
    )
    stale = manager.on_gateway_activity(
        context=context,
        activity_snapshot={"phase": "queued", "status": "queued"},
    )
    completed = manager.on_gateway_activity(
        context=context,
        activity_snapshot={"phase": "completed", "status": "completed"},
        terminal=True,
    )
    after_terminal = manager.on_gateway_activity(
        context=context,
        activity_snapshot={"phase": "running", "status": "working again"},
    )

    assert running is not None
    assert stale == running
    assert completed is not None and completed.terminal is True
    assert after_terminal == completed


@pytest.mark.asyncio
async def test_debounced_publish_runs_on_gateway_owning_event_loop(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0.01,
    )
    context = _context(status=status)
    owning_loop = asyncio.get_running_loop()
    owning_thread = threading.get_ident()

    manager.handle_command("bind release-card", context)
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={"phase": "running", "status": "working"},
    )
    await manager.wait_for_publishes()

    assert len(status.calls) == 1
    assert status.calls[0]["loop"] is owning_loop
    assert status.calls[0]["thread"] == owning_thread
    assert not hasattr(manager, "_pending_timers")


def test_unbound_lifecycle_event_does_not_create_a_card(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)

    state = manager.on_gateway_activity(
        context=_context(),
        activity_snapshot={"phase": "running", "status": "working"},
    )

    assert state is None
    assert manager.store.list_bindings() == []


def test_update_before_bind_is_rejected_without_orphaned_state(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path))

    response = manager.handle_command("close premature", _context())

    assert "not bound yet" in response
    assert manager.store.list_bindings() == []
    assert manager.current_state(
        "telegram:chat-1:topic-9:session-1",
        "telegram:chat-1:topic-9:session-1",
    ) is None


def test_concurrent_stores_preserve_unrelated_topic_bindings(tmp_path):
    plugin = _load_plugin()
    first = plugin.TaskCardStore(tmp_path)
    second = plugin.TaskCardStore(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(first.bind_topic, "topic-alpha", "alpha"),
            pool.submit(second.bind_topic, "topic-beta", "beta"),
        ]
        for future in futures:
            future.result()

    assert first.resolve_binding("topic-alpha") == "alpha"
    assert second.resolve_binding("topic-beta") == "beta"


def test_unrelated_activity_cannot_terminal_lock_active_background_card(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context()
    manager.handle_command("bind release-card", context)

    background = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "background",
            "task_id": "bg-1",
            "phase": "background-start",
            "status": "running",
        },
    )
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground",
            "phase": "completed",
            "status": "completed",
        },
        terminal=True,
    )
    after_unrelated = manager.current_state("release-card")
    completed = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "background",
            "task_id": "bg-1",
            "phase": "completed",
            "status": "completed",
        },
        terminal=True,
    )

    assert background is not None
    assert background.activity_id == "background:bg-1"
    assert after_unrelated == background
    assert completed is not None and completed.terminal is True


def test_global_manager_isolated_by_profile_home(tmp_path, monkeypatch):
    plugin = _load_plugin()
    default_home = tmp_path / "default"
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(plugin, "get_hermes_home", lambda: default_home)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: profiles / name,
    )

    alpha = _context(profile="alpha")
    beta = _context(profile="beta")
    plugin._handle_taskcard("bind shared", alpha)
    plugin._handle_taskcard("bind shared", beta)

    alpha_manager = plugin.get_manager(alpha)
    beta_manager = plugin.get_manager(beta)
    assert alpha_manager is not beta_manager
    assert alpha_manager.store.root == profiles / "alpha" / "plugins" / "task-card"
    assert beta_manager.store.root == profiles / "beta" / "plugins" / "task-card"
    assert alpha_manager.current_state("shared").revision == 1
    assert beta_manager.current_state("shared").revision == 1


def test_same_label_is_isolated_between_topics(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    first_context = _context(thread_id="topic-1")
    second_context = _context(thread_id="topic-2")
    first_topic = "telegram:chat-1:topic-1:session-1"
    second_topic = "telegram:chat-1:topic-2:session-1"

    manager.handle_command("bind shared", first_context)
    manager.handle_command("bind shared", second_context)
    manager.on_gateway_activity(
        context=first_context,
        activity_snapshot={
            "kind": "foreground",
            "phase": "completed",
            "status": "completed",
        },
        terminal=True,
    )

    first = manager.current_state("shared", first_topic)
    second = manager.current_state("shared", second_topic)
    assert first is not None and first.terminal is True
    assert second is not None and second.terminal is False
    assert first.topic_identity != second.topic_identity

    manager.handle_command("bind shared", first_context)
    rebound = manager.current_state("shared", first_topic)
    assert rebound is not None and rebound.terminal is False
    assert rebound.revision == 1
    assert rebound.generation != first.generation
    assert manager.current_state("shared", second_topic) == second

    manager.handle_command("reset", first_context)
    assert manager.current_state("shared", first_topic) is None
    assert manager.current_state("shared", second_topic) == second
