"""Focused tests for the platform-neutral status upsert contract."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, SendResult


def _adapter(results: list[SendResult] | None = None) -> Any:
    class DummyAdapter:
        pass

    adapter = cast(Any, DummyAdapter())
    adapter.send_or_update_status = AsyncMock(
        side_effect=results
        or [SendResult(success=True, message_id="m-1")]
    )
    return adapter


@pytest.mark.asyncio
async def test_status_upsert_publishes_first_revision():
    adapter = _adapter()

    result = await BasePlatformAdapter.upsert_status(
        adapter,
        "chat-1",
        "taskcard",
        "working",
        revision=1,
        metadata={"thread_id": "thread-9"},
    )

    assert result.success is True
    assert result.message_id == "m-1"
    adapter.send_or_update_status.assert_awaited_once_with(
        "chat-1",
        "taskcard",
        "working",
        metadata={"thread_id": "thread-9"},
    )


@pytest.mark.asyncio
async def test_status_upsert_ignores_stale_and_equal_revisions():
    adapter = _adapter()

    first = await BasePlatformAdapter.upsert_status(
        adapter, "chat-1", "taskcard", "working", revision=4
    )
    stale = await BasePlatformAdapter.upsert_status(
        adapter, "chat-1", "taskcard", "late", revision=3
    )
    equal = await BasePlatformAdapter.upsert_status(
        adapter, "chat-1", "taskcard", "conflict", revision=4
    )

    assert first.success is True
    assert stale.success is True
    assert stale.message_id == "m-1"
    assert stale.raw_response == {"status": "noop", "reason": "stale_revision"}
    assert equal.raw_response == {"status": "noop", "reason": "stale_revision"}
    adapter.send_or_update_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_upsert_suppresses_unchanged_content_and_advances_revision():
    adapter = _adapter()

    await BasePlatformAdapter.upsert_status(
        adapter, "chat-1", "taskcard", "working", revision=1
    )
    unchanged = await BasePlatformAdapter.upsert_status(
        adapter, "chat-1", "taskcard", "working", revision=2
    )
    stale_after_noop = await BasePlatformAdapter.upsert_status(
        adapter, "chat-1", "taskcard", "done", revision=2
    )

    assert unchanged.raw_response == {"status": "noop", "reason": "unchanged_content"}
    assert stale_after_noop.raw_response == {"status": "noop", "reason": "stale_revision"}
    adapter.send_or_update_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_upsert_keeps_topic_state_isolated():
    adapter = _adapter(
        [
            SendResult(success=True, message_id="m-1"),
            SendResult(success=True, message_id="m-2"),
        ]
    )

    await BasePlatformAdapter.upsert_status(
        adapter,
        "chat-1",
        "taskcard",
        "working",
        revision=1,
        metadata={"thread_id": "thread-1"},
    )
    result = await BasePlatformAdapter.upsert_status(
        adapter,
        "chat-1",
        "taskcard",
        "working",
        revision=1,
        metadata={"thread_id": "thread-2"},
    )

    assert result.message_id == "m-2"
    assert adapter.send_or_update_status.await_count == 2


@pytest.mark.asyncio
async def test_status_upsert_does_not_advance_state_after_failed_publish():
    adapter = _adapter(
        [
            SendResult(success=False, error="offline"),
            SendResult(success=True, message_id="m-2"),
        ]
    )

    failed = await BasePlatformAdapter.upsert_status(
        adapter, "chat-1", "taskcard", "working", revision=1
    )
    retried = await BasePlatformAdapter.upsert_status(
        adapter, "chat-1", "taskcard", "working", revision=1
    )

    assert failed.success is False
    assert retried.success is True
    assert adapter.send_or_update_status.await_count == 2


@pytest.mark.asyncio
async def test_status_upsert_unversioned_publish_preserves_revision_floor():
    """An unversioned update must not erase the last accepted revision."""
    adapter = _adapter(
        [
            SendResult(success=True, message_id="m-1"),
            SendResult(success=True, message_id="m-1"),
        ]
    )

    await BasePlatformAdapter.upsert_status(
        adapter, "chat-1", "taskcard", "starting", revision=5
    )
    await BasePlatformAdapter.upsert_status(
        adapter, "chat-1", "taskcard", "working"
    )
    stale = await BasePlatformAdapter.upsert_status(
        adapter, "chat-1", "taskcard", "late", revision=4
    )

    assert stale.raw_response == {"status": "noop", "reason": "stale_revision"}
    assert adapter.send_or_update_status.await_count == 2


@pytest.mark.asyncio
async def test_status_upsert_bounds_adapter_state_and_locks():
    """Unique plugin keys cannot grow adapter status state without limit."""
    adapter = _adapter(
        [
            SendResult(success=True, message_id="m-1"),
            SendResult(success=True, message_id="m-2"),
            SendResult(success=True, message_id="m-3"),
        ]
    )
    adapter._status_upsert_cache_max = 2

    for index in range(3):
        await BasePlatformAdapter.upsert_status(
            adapter,
            "chat-1",
            f"status-{index}",
            f"content-{index}",
            revision=1,
        )

    assert len(adapter._status_upsert_states) == 2
    assert len(adapter._status_upsert_locks) == 2
    assert ("chat-1", "", "status-0", "") not in adapter._status_upsert_states


@pytest.mark.asyncio
async def test_status_upsert_generation_allows_revision_restart():
    adapter = _adapter(
        [
            SendResult(success=True, message_id="first"),
            SendResult(success=True, message_id="second"),
        ]
    )

    await BasePlatformAdapter.upsert_status(
        adapter,
        "chat",
        "taskcard",
        "generation one",
        revision=3,
        metadata={"thread_id": "topic", "generation": "one"},
    )
    restarted = await BasePlatformAdapter.upsert_status(
        adapter,
        "chat",
        "taskcard",
        "generation two",
        revision=1,
        metadata={"thread_id": "topic", "generation": "two"},
    )

    assert restarted.success is True
    assert adapter.send_or_update_status.await_count == 2