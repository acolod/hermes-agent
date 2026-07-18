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
from unittest.mock import AsyncMock

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
            registered.setdefault("hooks", []).append((name, handler))

    plugin.register(Context())

    name, handler, metadata = registered["command"]
    assert name == "taskcard"
    assert "context" in inspect.signature(handler).parameters
    assert metadata["args_hint"].startswith("[show|bind")
    assert {name for name, _ in registered["hooks"]} == {"gateway_activity", "pre_llm_call"}


def test_public_renderer_is_a_concise_checklist_without_diagnostic_metadata(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context()
    manager.handle_command("bind Improve task card", context)
    state = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground",
            "phase": "running",
            "status": "running",
            "summary": "Implementing the focused change",
            "terminal": False,
        },
    )

    rendered = plugin.render_task_card(state)

    assert rendered == (
        "📋 **Active task**\n"
        "**Improve task card**\n\n"
        "- 🔄 Implementing the focused change"
    )
    for hidden in (
        state.binding,
        state.revision_hash,
        state.topic_identity,
        state.session_key,
        "binding   :",
        "revision  :",
        "route     :",
        "activity  :",
    ):
        if hidden == state.binding:
            continue
        assert hidden not in rendered


def test_structured_kimi_todos_render_as_bounded_claude_style_checklist(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context()
    manager.handle_command("bind Improve checklist", context)

    state = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground",
            "phase": "working",
            "status": "running",
            "summary": "Implementing checklist support",
            "todos": [
                {"id": "inspect", "content": "Inspect existing state", "status": "completed"},
                {"id": "implement", "content": "Implement item model", "status": "in_progress"},
                {"id": "test", "content": "Run focused tests", "status": "pending"},
                {"id": "blocked", "content": "Await external input", "status": "blocked"},
                {"id": "skip", "content": "Skip deprecated path", "status": "cancelled"},
                {"id": "fail", "content": "Record failed publication", "status": "failed"},
            ],
        },
    )

    assert [(item.item_id, item.status) for item in state.items] == [
        ("inspect", "complete"),
        ("implement", "active"),
        ("test", "pending"),
        ("blocked", "blocked"),
        ("skip", "skipped"),
        ("fail", "failed"),
    ]
    assert plugin.render_task_card(state) == (
        "📋 **Active task**\n"
        "**Improve checklist**\n\n"
        "- ✅ Inspect existing state\n"
        "- ▶️ Implement item model\n"
        "- ⬜ Run focused tests\n"
        "- ⚠️ Await external input\n"
        "- ➖ Skip deprecated path\n"
        "- ❌ Record failed publication"
    )


def test_relay_task_plan_metadata_populates_structured_card_items(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context()
    manager.handle_command("bind Relay plan", context)

    state = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:task-1",
            "phase": "working",
            "status": "running",
            "summary": "Relay working",
            "metadata": {
                "task_plan": [
                    {"id": "draft", "title": "Draft response", "status": "done"},
                    {"id": "review", "title": "Review evidence", "status": "running"},
                ],
            },
        },
    )

    assert [(item.item_id, item.label, item.status) for item in state.items] == [
        ("draft", "Draft response", "complete"),
        ("review", "Review evidence", "active"),
    ]


def test_missing_source_ids_are_stable_across_snapshot_reordering():
    plugin = _load_plugin()
    first = plugin._task_items_from_snapshot(
        {"todos": [{"content": "Inspect state"}, {"content": "Run tests"}]}
    )
    reordered = plugin._task_items_from_snapshot(
        {"todos": [{"content": "Run tests"}, {"content": "Inspect state"}]}
    )

    assert {item.label: item.item_id for item in first} == {
        item.label: item.item_id for item in reordered
    }


def test_structured_items_are_bounded_to_the_highest_priority_sixteen():
    plugin = _load_plugin()
    items = plugin._task_items_from_snapshot(
        {"todos": [{"id": f"item-{index}", "content": f"Step {index}"} for index in range(20)]}
    )

    assert len(items) == 16
    assert [item.item_id for item in items] == [f"item-{index}" for index in range(16)]


def test_late_item_update_cannot_regress_a_completed_item(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context()
    manager.handle_command("bind Safe checklist", context)

    completed = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:task-1",
            "phase": "working",
            "status": "running",
            "summary": "Relay working",
            "task_items": [{"id": "evidence", "label": "Collect evidence", "status": "completed"}],
        },
    )
    late = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:task-1",
            "phase": "working",
            "status": "running",
            "summary": "Late relay progress",
            "task_items": [{"id": "evidence", "label": "Collect evidence", "status": "in_progress"}],
        },
    )

    assert completed.items[0].status == "complete"
    assert late.revision == completed.revision + 1
    assert late.items[0].status == "complete"


@pytest.mark.asyncio
async def test_checklist_updates_reuse_the_existing_platform_message(tmp_path):
    plugin = _load_plugin()

    class BoundMessageStatus(_CaptureStatus):
        async def upsert_status(self, *args, **kwargs):
            await super().upsert_status(*args, **kwargs)
            return SimpleNamespace(success=True, message_id="bound-message")

    status = BoundMessageStatus()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context(status=status)
    manager.handle_command("bind Checklist", context)
    await manager.wait_for_publishes()
    first = manager.current_state("Checklist")

    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground",
            "phase": "working",
            "status": "running",
            "summary": "Implementing checklist",
            "todos": [{"id": "implement", "content": "Implement checklist", "status": "in_progress"}],
        },
    )
    await manager.wait_for_publishes()
    updated = manager.current_state("Checklist")

    assert first.platform_message_id == "bound-message"
    assert updated.platform_message_id == "bound-message"
    assert status.calls[-1]["metadata"]["status_message_id"] == "bound-message"


def test_single_row_fallback_is_preserved_without_structured_items(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context()
    manager.handle_command("bind Fallback", context)

    state = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground",
            "phase": "working",
            "status": "running",
            "summary": "No structured plan available",
        },
    )

    assert state.items == ()
    assert plugin.render_task_card(state).endswith("- 🔄 No structured plan available")


def test_foreground_todo_creates_task_keyed_card_without_conversation_binding(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context(status=_CaptureStatus())

    state = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground", "task_id": "fg_alpha", "activity_id": "foreground:fg_alpha",
            "generation": "gen-alpha", "revision": 2, "phase": "working", "status": "running",
            "task_items": [{"id": "one", "content": "First step", "status": "in_progress"}],
        },
    )

    assert state is not None
    assert state.binding == "foreground:fg_alpha"
    assert manager.store.resolve_binding(state.topic_identity) is None
    assert manager.current_state("foreground:fg_alpha", state.topic_identity) == state


def test_foreground_public_card_uses_todo_title_not_internal_task_id(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    state = manager.on_gateway_activity(
        context=_context(),
        activity_snapshot={
            "kind": "foreground", "task_id": "fg_secret", "activity_id": "foreground:fg_secret",
            "generation": "gen", "revision": 2, "phase": "working", "status": "running",
            "task_items": [{"id": "one", "content": "Check gateway health", "status": "in_progress"}],
        },
    )

    rendered = plugin.render_task_card(state)
    assert "Check gateway health" in rendered
    assert "fg_secret" not in rendered


def test_foreground_without_todo_does_not_create_task_card(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)

    state = manager.on_gateway_activity(
        context=_context(status=_CaptureStatus()),
        activity_snapshot={
            "kind": "foreground", "task_id": "fg_plain", "activity_id": "foreground:fg_plain",
            "generation": "gen-plain", "revision": 1, "phase": "foreground-start", "status": "running",
        },
    )

    assert state is None
    assert list((tmp_path / "cards").glob("*.json")) == []


def test_public_renderer_hides_generated_binding_and_diagnostic_summary(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context()
    manager.handle_command("bind", context)
    binding = manager.store.resolve_binding("telegram:chat-1:topic-9:session-1")
    state = manager.current_state(binding)
    state = plugin.replace(
        state,
        summary=(
            f"route: telegram/chat-1; session_key={state.session_key}; "
            f"hash={state.revision_hash}"
        ),
    )

    rendered = plugin.render_task_card(state)

    assert "**Current conversation**" in rendered
    assert binding not in rendered
    assert state.session_key not in rendered
    assert state.revision_hash not in rendered
    assert "route:" not in rendered
    assert "session_key=" not in rendered
    assert "hash=" not in rendered
    assert rendered.endswith("- 🔄 Task in progress")


def test_debug_subcommand_keeps_the_existing_technical_renderer(tmp_path):
    plugin = _load_plugin()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context()
    manager.handle_command("bind Improve task card", context)

    rendered = manager.handle_command("debug", context)

    assert "╭─ Task Card" in rendered
    assert "│ binding   : Improve task card" in rendered
    assert "│ revision  : rev 1  hash " in rendered
    assert "│ route     : telegram · chat-1 · topic-9 · session-1" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_args", ["", "refresh"])
async def test_default_command_refreshes_existing_card_without_posting_snapshot(
    tmp_path,
    raw_args,
):
    plugin = _load_plugin()

    class FixedMessageStatus(_CaptureStatus):
        async def upsert_status(self, *args, **kwargs):
            result = await super().upsert_status(*args, **kwargs)
            return SimpleNamespace(success=result.success, message_id="bound-message")

    status = FixedMessageStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
    )
    context = _context(status=status)
    manager.handle_command("bind Improve task card", context)
    await manager.wait_for_publishes()
    original = manager.current_state("Improve task card")
    assert original is not None
    original_message_id = original.platform_message_id

    response = manager.handle_command(raw_args, context)
    await manager.wait_for_publishes()

    refreshed = manager.current_state("Improve task card")
    assert response == "Task card refreshed — see the pinned card in this conversation."
    assert refreshed is not None
    assert refreshed.revision == original.revision
    assert refreshed.platform_message_id == original_message_id
    assert status.calls[-1]["revision"] is None
    assert status.calls[-1]["metadata"]["status_message_id"] == original_message_id
    assert status.calls[-1]["content"].startswith("📋 **Active task**")
    assert "binding   :" not in status.calls[-1]["content"]


@pytest.mark.asyncio
async def test_refresh_does_not_consume_next_external_lifecycle_revision(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
    )
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()
    started = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:task-123",
            "generation": "relay-generation-1",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "Relay started",
            "terminal": False,
        },
    )
    await manager.wait_for_publishes()

    manager.handle_command("refresh", context)
    await manager.wait_for_publishes()
    refreshed = manager.current_state("Relay")
    working = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:task-123",
            "generation": "relay-generation-1",
            "revision": 2,
            "phase": "working",
            "status": "running",
            "summary": "Relay working",
            "terminal": False,
        },
    )

    assert started is not None and started.revision == 1
    assert refreshed is not None and refreshed.revision == 1
    assert working is not None and working.revision == 2
    assert working.phase == "working"


@pytest.mark.asyncio
async def test_failed_refresh_does_not_advance_persisted_state(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus(outcomes=[True, False])
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
        publish_retry_attempts=1,
    )
    context = _context(status=status)
    manager.handle_command("bind Improve task card", context)
    await manager.wait_for_publishes()
    before = manager.current_state("Improve task card")
    assert before is not None
    before = plugin.replace(before, content_hash="legacy-renderer-hash")
    key = (before.topic_identity, before.binding)
    manager._state_cache[key] = before
    manager.store.save(before)

    response = manager.handle_command("refresh", context)
    await manager.wait_for_publishes()

    assert response == "Task card refreshed — see the pinned card in this conversation."
    assert manager.current_state("Improve task card") == before
    assert manager.store.load("Improve task card", before.topic_identity) == before


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
async def test_second_foreground_turn_reopens_terminal_card_on_same_message(tmp_path):
    plugin = _load_plugin()

    class FixedMessageStatus(_CaptureStatus):
        async def upsert_status(self, *args, **kwargs):
            result = await super().upsert_status(*args, **kwargs)
            return SimpleNamespace(success=result.success, message_id="15438")

    status = FixedMessageStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
    )
    context = _context(status=status)
    manager.handle_command("bind Smoke test", context)
    await manager.wait_for_publishes()

    first_started = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground",
            "phase": "foreground-start",
            "status": "running",
            "summary": "Foreground task started",
            "terminal": False,
        },
    )
    await manager.wait_for_publishes()
    first_completed = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground",
            "phase": "completed",
            "status": "completed",
            "summary": "Foreground task completed",
            "terminal": True,
        },
    )
    await manager.wait_for_publishes()
    first_completed = manager.current_state("Smoke test", first_completed.topic_identity)

    assert first_started is not None
    assert first_completed is not None
    assert first_completed.terminal is True
    assert first_completed.revision == 3
    assert first_completed.platform_message_id == "15438"
    first_generation = first_completed.generation

    second_started = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground",
            "phase": "foreground-start",
            "status": "running",
            "summary": "Foreground task started",
            "terminal": False,
        },
    )
    await manager.wait_for_publishes()
    second_completed = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground",
            "phase": "completed",
            "status": "completed",
            "summary": "Foreground task completed",
            "terminal": True,
        },
    )
    await manager.wait_for_publishes()

    assert second_started is not None
    assert second_started.terminal is False
    assert second_started.phase == "foreground-start"
    assert second_started.generation != first_generation
    assert second_started.revision == 1
    assert second_started.platform_message_id == "15438"
    assert second_completed is not None
    assert second_completed.terminal is True
    assert second_completed.phase == "completed"
    assert second_completed.generation == second_started.generation
    assert second_completed.revision == 2
    assert second_completed.platform_message_id == "15438"
    assert [call["revision"] for call in status.calls[-2:]] == [1, 2]
    assert all(call["metadata"]["status_message_id"] == "15438" for call in status.calls[-2:])


@pytest.mark.asyncio
async def test_new_foreground_generation_clears_prior_items_but_same_generation_omission_preserves_them(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context(status=status)
    manager.handle_command("bind Current conversation", context)
    await manager.wait_for_publishes()

    seeded = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground", "generation": "old", "revision": 1,
            "phase": "started", "status": "running", "task_items": [
                {"id": "old", "label": "Prior task", "status": "completed"}
            ],
        },
    )
    terminal = manager.on_gateway_activity(
        context=context,
        activity_snapshot={"kind": "foreground", "generation": "old", "revision": 2,
                           "phase": "completed", "status": "completed", "terminal": True},
    )
    new_started = manager.on_gateway_activity(
        context=context,
        activity_snapshot={"kind": "foreground", "generation": "new", "revision": 1,
                           "phase": "foreground-start", "status": "running"},
    )
    same_generation = manager.on_gateway_activity(
        context=context,
        activity_snapshot={"kind": "foreground", "generation": "new", "revision": 2,
                           "phase": "working", "status": "running", "task_items": [
                               {"id": "new", "label": "New task", "status": "in_progress"}
                           ]},
    )
    terminal_same_generation = manager.on_gateway_activity(
        context=context,
        activity_snapshot={"kind": "foreground", "generation": "new", "revision": 3,
                           "phase": "completed", "status": "completed", "terminal": True},
    )

    assert seeded.items and terminal.items
    assert new_started.items == ()
    assert [item.item_id for item in same_generation.items] == ["new"]
    assert [item.item_id for item in terminal_same_generation.items] == ["new"]


@pytest.mark.asyncio
async def test_external_gateway_activity_preserves_generation_revision_and_correlation(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
    )
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()

    started = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:task-123",
            "generation": "relay-generation-1",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "Relay started",
            "terminal": False,
        },
    )
    await manager.wait_for_publishes()
    completed = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:task-123",
            "generation": "relay-generation-1",
            "revision": 2,
            "phase": "completed",
            "status": "completed",
            "summary": "Relay completed",
            "terminal": True,
        },
    )
    await manager.wait_for_publishes()

    assert started is not None
    assert started.generation == "relay-generation-1"
    assert started.revision == 1
    assert started.activity_id == "relay:task-123"
    assert completed is not None
    assert completed.generation == "relay-generation-1"
    assert completed.revision == 2
    assert completed.activity_id == "relay:task-123"
    assert [call["revision"] for call in status.calls[-2:]] == [1, 2]


@pytest.mark.asyncio
async def test_external_gateway_activity_suppresses_stale_equal_and_unchanged_content(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
    )
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:task-123",
            "generation": "relay-generation-1",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "Relay started",
            "terminal": False,
        },
    )
    await manager.wait_for_publishes()
    event = {
        "kind": "relay",
        "activity_id": "relay:task-123",
        "generation": "relay-generation-1",
        "revision": 2,
        "phase": "working",
        "status": "running",
        "summary": "Relay working",
        "terminal": False,
    }
    accepted = manager.on_gateway_activity(context=context, activity_snapshot=event)
    await manager.wait_for_publishes()
    calls_after_accepted = len(status.calls)

    equal = manager.on_gateway_activity(context=context, activity_snapshot=event)
    stale = manager.on_gateway_activity(
        context=context,
        activity_snapshot={**event, "revision": 1, "summary": "stale"},
    )
    unchanged = manager.on_gateway_activity(
        context=context,
        activity_snapshot={**event, "revision": 3},
    )
    await manager.wait_for_publishes()

    persisted = manager.current_state("Relay", accepted.topic_identity)
    assert persisted is not None and persisted.revision == 2
    assert equal == persisted
    assert stale == persisted
    assert unchanged == persisted
    assert len(status.calls) == calls_after_accepted


@pytest.mark.asyncio
async def test_external_gateway_activity_rejects_superseded_generation(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()

    for event in (
        {
            "kind": "relay",
            "activity_id": "relay:a",
            "generation": "generation-a",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "A started",
            "terminal": False,
        },
        {
            "kind": "relay",
            "activity_id": "relay:a",
            "generation": "generation-a",
            "revision": 2,
            "phase": "completed",
            "status": "completed",
            "summary": "A completed",
            "terminal": True,
        },
        {
            "kind": "relay",
            "activity_id": "relay:b",
            "generation": "generation-b",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "B started",
            "terminal": False,
        },
    ):
        manager.on_gateway_activity(context=context, activity_snapshot=event)
        await manager.wait_for_publishes()

    calls_before_delayed = len(status.calls)
    delayed = manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:a",
            "generation": "generation-a",
            "revision": 3,
            "phase": "failed",
            "status": "failed",
            "summary": "delayed A failure",
            "terminal": True,
        },
    )
    await manager.wait_for_publishes()

    current = manager.current_state("Relay")
    assert delayed == current
    assert current is not None and current.generation == "generation-b"
    assert current.phase == "started"
    assert len(status.calls) == calls_before_delayed


@pytest.mark.asyncio
async def test_status_ingress_publication_ack_tracks_actual_publish_result(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
        publish_retry_seconds=0,
        publish_retry_attempts=1,
    )
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()

    success_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = success_ack
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:ack-success",
            "generation": "ack-success",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "started",
        },
    )
    await manager.wait_for_publishes()
    assert success_ack.result() is True

    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:ack-success",
            "generation": "ack-success",
            "revision": 2,
            "phase": "completed",
            "status": "completed",
            "summary": "completed",
            "terminal": True,
        },
    )
    await manager.wait_for_publishes()

    failure_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = failure_ack
    status.outcomes = [False]
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:ack-failure",
            "generation": "ack-failure",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "will fail",
        },
    )
    await manager.wait_for_publishes()
    assert failure_ack.result() is False


@pytest.mark.asyncio
async def test_status_ingress_debounce_transfers_ack_to_coalesced_revision(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0.02,
        publish_retry_seconds=0,
    )
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()
    calls_before = len(status.calls)

    first_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = first_ack
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:coalesced",
            "generation": "coalesced-generation",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "started",
        },
    )

    second_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = second_ack
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:coalesced",
            "generation": "coalesced-generation",
            "revision": 2,
            "phase": "working",
            "status": "running",
            "summary": "working",
        },
    )
    await manager.wait_for_publishes()

    assert first_ack.result() is True
    assert second_ack.result() is True
    assert len(status.calls) == calls_before + 1
    assert manager.current_state("Relay").revision == 2


@pytest.mark.asyncio
async def test_duplicate_status_ingress_waits_for_pending_publish_failure(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0.02,
        publish_retry_seconds=0,
        publish_retry_attempts=1,
    )
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()
    status.outcomes = [False]
    event = {
        "kind": "relay",
        "activity_id": "relay:duplicate",
        "generation": "duplicate-generation",
        "revision": 1,
        "phase": "started",
        "status": "running",
        "summary": "started",
    }

    first_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = first_ack
    manager.on_gateway_activity(context=context, activity_snapshot=event)
    duplicate_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = duplicate_ack
    manager.on_gateway_activity(context=context, activity_snapshot=event)
    assert not first_ack.done() and not duplicate_ack.done()

    await manager.wait_for_publishes()

    assert first_ack.result() is False
    assert duplicate_ack.result() is False


@pytest.mark.asyncio
async def test_immediate_status_ingress_coalesces_without_stranding_ack(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0)
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()
    calls_before = len(status.calls)

    first_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = first_ack
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:immediate",
            "generation": "immediate-generation",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "started",
        },
    )
    second_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = second_ack
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:immediate",
            "generation": "immediate-generation",
            "revision": 2,
            "phase": "working",
            "status": "running",
            "summary": "working",
        },
    )
    await manager.wait_for_publishes()

    assert first_ack.result() is True
    assert second_ack.result() is True
    assert len(status.calls) == calls_before + 1
    assert manager.current_state("Relay").revision == 2


@pytest.mark.asyncio
async def test_internal_lifecycle_state_survives_publish_failure(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
        publish_retry_seconds=0,
        publish_retry_attempts=2,
    )
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()

    status.outcomes = [False, False]
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "background",
            "activity_id": "background:internal",
            "generation": "internal-generation",
            "phase": "background-start",
            "status": "running",
            "summary": "internal start",
        },
    )
    await manager.wait_for_publishes()

    failed_delivery_state = manager.current_state("Relay")
    assert failed_delivery_state is not None
    assert failed_delivery_state.generation == "internal-generation"
    assert failed_delivery_state.phase == "background-start"

    status.outcomes = [True]
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "background",
            "activity_id": "background:internal",
            "generation": "internal-generation",
            "phase": "completed",
            "status": "completed",
            "summary": "internal complete",
        },
        terminal=True,
    )
    await manager.wait_for_publishes()

    completed = manager.current_state("Relay")
    assert completed is not None
    assert completed.phase == "completed"
    assert completed.terminal is True


@pytest.mark.asyncio
async def test_newer_publish_failure_restores_inflight_success_baseline(tmp_path):
    plugin = _load_plugin()

    class BlockingStatus(_CaptureStatus):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def upsert_status(self, status_key, content, *, revision=None, metadata=None):
            self.calls.append(
                {
                    "status_key": status_key,
                    "content": content,
                    "revision": revision,
                    "metadata": metadata,
                }
            )
            if metadata and metadata.get("generation") == "inflight-generation":
                if revision == 1:
                    self.started.set()
                    await self.release.wait()
                    return SimpleNamespace(success=True, message_id="message-inflight")
                return SimpleNamespace(success=False, message_id=None)
            return SimpleNamespace(success=True, message_id="message-bind")

    status = BlockingStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
        publish_retry_seconds=0,
        publish_retry_attempts=1,
    )
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()

    first_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = first_ack
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:inflight",
            "generation": "inflight-generation",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "started",
        },
    )
    await status.started.wait()

    second_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = second_ack
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:inflight",
            "generation": "inflight-generation",
            "revision": 2,
            "phase": "working",
            "status": "running",
            "summary": "working",
        },
    )
    status.release.set()
    await manager.wait_for_publishes()

    current = manager.current_state("Relay")
    assert current is not None
    assert current.revision == 1
    assert current.summary == "started"
    assert current.platform_message_id == "message-inflight"
    assert first_ack.result() is False
    assert second_ack.result() is False


@pytest.mark.asyncio
async def test_rebind_cancels_pending_status_ingress_transaction(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(plugin.TaskCardStore(tmp_path), debounce_seconds=0.05)
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()

    pending_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = pending_ack
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:pending-rebind",
            "generation": "pending-rebind-generation",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "pending",
        },
    )
    context.metadata.pop("_status_ingress_ack")
    manager.handle_command("bind Replacement", context)
    await manager.wait_for_publishes()

    assert pending_ack.result() is False
    assert manager.current_state("Relay") is None
    assert manager.current_state("Replacement") is not None


@pytest.mark.asyncio
async def test_reset_discards_pending_rollback_before_rebind_failure(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
        publish_retry_seconds=0,
        publish_retry_attempts=1,
    )
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()
    old_state = manager.current_state("Relay")
    assert old_state is not None

    manager.debounce_seconds = 30
    pending_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = pending_ack
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "relay",
            "activity_id": "relay:pending",
            "generation": "pending-generation",
            "revision": 1,
            "phase": "started",
            "status": "running",
            "summary": "pending",
        },
    )
    context.metadata.pop("_status_ingress_ack")
    manager.handle_command("reset Relay", context)
    assert pending_ack.result() is False
    assert manager.store.load("Relay") is None

    manager.debounce_seconds = 0
    status.outcomes = [False]
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()
    rebound = manager.current_state("Relay")
    assert rebound is not None
    assert rebound != old_state
    assert rebound.phase == "command"


@pytest.mark.asyncio
async def test_failed_external_edit_rolls_back_and_same_revision_can_retry_without_replacement(tmp_path):
    plugin = _load_plugin()
    status = _CaptureStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
        publish_retry_seconds=0,
        publish_retry_attempts=2,
    )
    context = _context(status=status)
    manager.handle_command("bind Relay", context)
    await manager.wait_for_publishes()
    baseline = manager.current_state("Relay")
    assert baseline is not None and baseline.platform_message_id == "message-1"

    event = {
        "kind": "relay",
        "activity_id": "relay:task-failure",
        "generation": "relay-generation-failure",
        "revision": 1,
        "phase": "started",
        "status": "running",
        "summary": "Relay working",
        "terminal": False,
    }
    status.outcomes = [False, False]
    failed_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = failed_ack
    manager.on_gateway_activity(context=context, activity_snapshot=event)
    await manager.wait_for_publishes()

    assert manager.current_state("Relay") == baseline
    assert failed_ack.result() is False
    assert manager.store.load("Relay") == baseline
    assert [call["metadata"]["status_message_id"] for call in status.calls[-2:]] == [
        "message-1",
        "message-1",
    ]

    status.outcomes = [True]
    retry_ack = asyncio.get_running_loop().create_future()
    context.metadata["_status_ingress_ack"] = retry_ack
    retried = manager.on_gateway_activity(context=context, activity_snapshot=event)
    await manager.wait_for_publishes()
    persisted = manager.current_state("Relay")

    assert retried is not None and retried.revision == 1
    assert persisted is not None
    assert persisted.generation == "relay-generation-failure"
    assert persisted.revision == 1
    assert persisted.platform_message_id == "message-4"
    assert status.calls[-1]["metadata"]["status_message_id"] == "message-1"
    assert retry_ack.result() is True


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
async def test_publish_retry_preserves_message_and_honors_retry_after(tmp_path, monkeypatch):
    plugin = _load_plugin()

    class RetryAfterStatus(_CaptureStatus):
        responses: list[Any]

        def __init__(self) -> None:
            super().__init__()
            self.responses = [SimpleNamespace(success=True, message_id="15499")]

        async def upsert_status(self, *args, **kwargs):
            self.calls.append({
                "status_key": args[0],
                "content": args[1],
                "revision": kwargs.get("revision"),
                "metadata": kwargs.get("metadata"),
                "loop": asyncio.get_running_loop(),
                "thread": threading.get_ident(),
            })
            return self.responses.pop(0)

    status = RetryAfterStatus()
    manager = plugin.TaskCardManager(
        plugin.TaskCardStore(tmp_path),
        debounce_seconds=0,
        publish_retry_seconds=0.25,
    )
    context = _context(status=status)
    manager.handle_command("bind Smoke test", context)
    await manager.wait_for_publishes()

    status.responses = [
        SimpleNamespace(
            success=False,
            message_id="15499",
            retry_after=2.0,
        ),
        SimpleNamespace(success=True, message_id="15499"),
    ]
    sleep = AsyncMock()
    monkeypatch.setattr(plugin.asyncio, "sleep", sleep)
    manager.on_gateway_activity(
        context=context,
        activity_snapshot={
            "kind": "foreground",
            "phase": "foreground-start",
            "status": "running",
        },
    )
    await manager.wait_for_publishes()

    assert [call["metadata"]["status_message_id"] for call in status.calls[-2:]] == [
        "15499",
        "15499",
    ]
    assert all(
        call["metadata"]["preserve_status_message_id"] is True
        for call in status.calls[-2:]
    )
    sleep.assert_awaited_once_with(2.0)
    state = manager.current_state("Smoke test")
    assert state is not None and state.platform_message_id == "15499"


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
