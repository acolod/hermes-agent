"""Focused gateway lifecycle emissions consumed by observer plugins."""

from __future__ import annotations

import asyncio
import logging
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

    def _emit(**kwargs):
        if not kwargs.get("terminal"):
            return None
        acknowledgement = __import__("asyncio").get_running_loop().create_future()
        acknowledgement.set_result(True)
        return acknowledgement

    runner._emit_gateway_activity = MagicMock(side_effect=_emit)
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
async def test_foreground_lifecycle_uses_one_unique_task_id_for_start_and_terminal():
    runner = _runner()

    await runner._run_agent("do work", "", [], _source(), "session-1", session_key="telegram:chat-1:topic-9")

    started, terminal = runner._emit_gateway_activity.call_args_list
    task_id = started.kwargs["task_id"]
    assert task_id.startswith("fg_")
    assert terminal.kwargs["task_id"] == task_id
    assert runner._run_agent_inner.await_args.kwargs["task_card_task_id"] == task_id


@pytest.mark.asyncio
async def test_foreground_terminal_activity_carries_observed_todo_snapshot():
    todos = [{"id": "inspect", "content": "Inspect state", "status": "completed"}]
    runner = _runner(result={"final_response": "done", "task_items": todos, "task_items_observed": True})

    await runner._run_agent("do work", "", [], _source(), "session-1", session_key="telegram:chat-1:topic-9")

    terminal = runner._emit_gateway_activity.call_args_list[-1].kwargs
    assert terminal["task_items"] == todos


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
    snapshot = call.kwargs["activity_snapshot"]
    assert {key: snapshot[key] for key in ("kind", "phase", "status", "summary", "terminal")} == {
        "kind": "foreground",
        "phase": "foreground-start",
        "status": "running",
        "summary": "Started",
        "terminal": False,
    }
    assert snapshot["activity_id"] == "foreground:telegram:chat-1:topic-9"
    assert snapshot["revision"] == 1
    assert snapshot["generation"]
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


@pytest.mark.asyncio
async def test_terminal_publication_ack_true_is_quiet(caplog):
    runner = object.__new__(GatewayRunner)
    acknowledgement = asyncio.get_running_loop().create_future()
    acknowledgement.set_result(True)
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await runner._await_terminal_card_publication(acknowledgement)
    assert "Terminal Task Card publication" not in caplog.text


@pytest.mark.asyncio
async def test_terminal_publication_ack_false_warns(caplog):
    runner = object.__new__(GatewayRunner)
    acknowledgement = asyncio.get_running_loop().create_future()
    acknowledgement.set_result(False)
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await runner._await_terminal_card_publication(acknowledgement)
    assert "Terminal Task Card publication was not accepted" in caplog.text


@pytest.mark.asyncio
async def test_terminal_publication_ack_timeout_warns(monkeypatch, caplog):
    runner = object.__new__(GatewayRunner)
    acknowledgement = asyncio.get_running_loop().create_future()

    async def _timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr("gateway.run.asyncio.wait_for", _timeout)
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await runner._await_terminal_card_publication(acknowledgement)
    assert "Terminal Task Card publication timed out after 10s" in caplog.text
    acknowledgement.cancel()
