"""Focused gateway lifecycle emissions consumed by observer plugins."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id="chat-1",
        thread_id="topic-9",
    )


def _runner(result=None, error: Exception | None = None):
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._emit_gateway_activity = MagicMock()
    if error is None:
        runner._run_agent_inner = AsyncMock(return_value=result or {"final_response": "done"})
    else:
        runner._run_agent_inner = AsyncMock(side_effect=error)
    return runner


@pytest.mark.asyncio
async def test_foreground_agent_run_emits_start_and_completed_lifecycle():
    runner = _runner()
    source = _source()

    result = await runner._run_agent(
        "do work",
        "",
        [],
        source,
        "session-1",
        session_key="telegram:chat-1:topic-9",
    )

    assert result["final_response"] == "done"
    assert [call.kwargs["phase"] for call in runner._emit_gateway_activity.call_args_list] == [
        "foreground-start",
        "completed",
    ]
    terminal = runner._emit_gateway_activity.call_args_list[-1].kwargs
    assert terminal["terminal"] is True
    assert terminal["kind"] == "foreground"
    assert terminal["source"] is source


@pytest.mark.asyncio
async def test_foreground_agent_run_emits_failed_lifecycle_before_reraising():
    runner = _runner(error=RuntimeError("boom"))
    source = _source()

    with pytest.raises(RuntimeError, match="boom"):
        await runner._run_agent(
            "do work",
            "",
            [],
            source,
            "session-1",
            session_key="telegram:chat-1:topic-9",
        )

    assert [call.kwargs["phase"] for call in runner._emit_gateway_activity.call_args_list] == [
        "foreground-start",
        "failed",
    ]
    assert runner._emit_gateway_activity.call_args_list[-1].kwargs["terminal"] is True


@pytest.mark.asyncio
async def test_foreground_interrupted_result_emits_cancelled_lifecycle():
    runner = _runner(result={"interrupted": True, "final_response": ""})

    await runner._run_agent(
        "do work",
        "",
        [],
        _source(),
        "session-1",
        session_key="telegram:chat-1:topic-9",
    )

    assert [call.kwargs["phase"] for call in runner._emit_gateway_activity.call_args_list] == [
        "foreground-start",
        "cancelled",
    ]


@pytest.mark.asyncio
async def test_partial_foreground_result_emits_failed_lifecycle():
    runner = _runner(
        result={
            "completed": False,
            "partial": True,
            "error": "Response remained incomplete",
            "final_response": "Partial visible response",
        }
    )

    await runner._run_agent(
        "hello",
        "",
        [],
        _source(),
        "session-1",
        session_key="telegram:chat-1:topic-9",
    )

    assert [call.kwargs["phase"] for call in runner._emit_gateway_activity.call_args_list] == [
        "foreground-start",
        "failed",
    ]


def test_gateway_activity_helper_emits_sanitized_context(monkeypatch):
    runner = object.__new__(GatewayRunner)
    adapter = MagicMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    source = _source()
    captured = MagicMock(return_value=[])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", captured)

    runner._emit_gateway_activity(
        source=source,
        session_key="telegram:chat-1:topic-9",
        kind="foreground",
        phase="foreground-start",
        status="running",
        summary="Started",
        terminal=False,
    )

    captured.assert_called_once()
    call = captured.call_args
    assert call.args == ("gateway_activity",)
    context = call.kwargs["context"]
    assert context.command == "gateway_activity"
    assert context.origin.platform == "telegram"
    assert context.origin.chat_id == "chat-1"
    assert context.origin.thread_id == "topic-9"
    assert context.origin.session_key == "telegram:chat-1:topic-9"
    assert call.kwargs["activity_snapshot"] == {
        "kind": "foreground",
        "phase": "foreground-start",
        "status": "running",
        "summary": "Started",
        "terminal": False,
    }
    assert "gateway" not in call.kwargs
    assert "adapter" not in call.kwargs


def test_gateway_activity_helper_preserves_derived_reply_thread(monkeypatch):
    runner = object.__new__(GatewayRunner)
    adapter = MagicMock()
    runner.adapters = {Platform.SLACK: adapter}
    runner._thread_metadata_for_source = MagicMock(
        return_value={"thread_id": "derived-thread"}
    )
    source = SessionSource(
        platform=Platform.SLACK,
        user_id="user-1",
        chat_id="channel-1",
        thread_id=None,
    )
    captured = MagicMock(return_value=[])
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", captured)

    runner._emit_gateway_activity(
        source=source,
        session_key="slack:channel-1",
        kind="foreground",
        phase="foreground-start",
        status="running",
        summary="Started",
        terminal=False,
        event_message_id="event-123",
    )

    context = captured.call_args.kwargs["context"]
    assert context.origin.thread_id == "derived-thread"
    assert context.status.thread_id == "derived-thread"
    runner._thread_metadata_for_source.assert_called_once_with(source, "event-123")
