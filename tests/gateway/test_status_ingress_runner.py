from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.status_ingress import OriginRoute


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-100123",
        chat_type="group",
        thread_id="9",
        profile="default",
    )


def _event():
    return {
        "activity_kind": "relay",
        "activity_id": "relay:task-123",
        "generation": "gen-123",
        "revision": 2,
        "phase": "completed",
        "status": "completed",
        "summary": "Relay completed",
        "metadata": {"relay_path": "task"},
    }


def test_internal_gateway_activity_registers_opaque_origin_handle_in_context_metadata():
    runner = object.__new__(GatewayRunner)
    runner._status_ingress = SimpleNamespace(register_origin=MagicMock(return_value="origin_opaque"))
    runner._thread_metadata_for_source = MagicMock(
        return_value={"thread_id": "9", "reply_to_message_id": "42"}
    )
    runner._adapter_for_source = MagicMock(return_value=SimpleNamespace(upsert_status=MagicMock()))

    with patch("hermes_cli.plugins.invoke_hook") as invoke:
        runner._emit_gateway_activity(
            source=_source(),
            session_key="agent:main:telegram:group:-100123:42",
            kind="foreground",
            phase="foreground-start",
            status="running",
            summary="started",
            terminal=False,
            event_message_id="42",
        )

    route = runner._status_ingress.register_origin.call_args.args[0]
    assert route == OriginRoute(
        platform="telegram",
        chat_id="-100123",
        thread_id="9",
        session_key="agent:main:telegram:group:-100123:42",
        profile="default",
        chat_type="group",
        status_metadata={"thread_id": "9", "reply_to_message_id": "42"},
    )
    context = invoke.call_args.kwargs["context"]
    assert context.metadata["status_origin_handle"] == "origin_opaque"
    assert "chat_id" not in context.metadata
    assert "thread_id" not in context.metadata


@pytest.mark.asyncio
async def test_external_status_event_resolves_gateway_route_and_invokes_existing_hook():
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(upsert_status=MagicMock())
    runner._adapter_for_source = MagicMock(return_value=adapter)
    route = OriginRoute(
        platform="telegram",
        chat_id="-100123",
        thread_id="9",
        session_key="agent:main:telegram:group:-100123:42",
        profile="default",
        chat_type="group",
        status_metadata={"thread_id": "9", "reply_to_message_id": "42"},
    )

    def accept(*_args, **kwargs):
        kwargs["context"].metadata["_status_ingress_ack"].set_result(True)
        return [object()]

    with patch("hermes_cli.plugins.invoke_hook", side_effect=accept) as invoke:
        result = await runner._handle_status_ingress_event(route, _event())

    assert result == {
        "accepted": True,
        "activity_id": "relay:task-123",
        "generation": "gen-123",
        "revision": 2,
    }
    context = invoke.call_args.kwargs["context"]
    snapshot = invoke.call_args.kwargs["activity_snapshot"]
    assert context.origin.platform == "telegram"
    assert context.origin.chat_id == "-100123"
    assert context.origin.thread_id == "9"
    assert context.origin.session_key == "agent:main:telegram:group:-100123:42"
    assert context.status is not None
    assert context.metadata["surface"] == "status_ingress"
    assert context.metadata["kind"] == "relay"
    assert context.metadata["external_metadata"] == {"relay_path": "task"}
    assert context.metadata["_status_ingress_ack"].result() is True
    assert snapshot == {
        "kind": "relay",
        "activity_id": "relay:task-123",
        "generation": "gen-123",
        "revision": 2,
        "phase": "completed",
        "status": "completed",
        "summary": "Relay completed",
        "terminal": True,
        "metadata": {"relay_path": "task"},
    }
    assert invoke.call_args.kwargs["terminal"] is True


@pytest.mark.asyncio
async def test_status_ingress_lifecycle_helpers_start_and_stop_once(tmp_path):
    runner = object.__new__(GatewayRunner)
    calls = []

    async def start():
        calls.append("start")

    async def stop():
        calls.append("stop")

    ingress = SimpleNamespace(start=start, stop=stop)
    runner._status_ingress = ingress

    await runner._start_status_ingress()
    await runner._stop_status_ingress()

    # The helpers use the existing instance rather than creating parallel servers.
    assert runner._status_ingress is ingress
    assert calls == ["start", "stop"]


@pytest.mark.asyncio
async def test_external_status_event_fails_closed_when_no_plugin_accepts_it():
    runner = object.__new__(GatewayRunner)
    runner._adapter_for_source = MagicMock(
        return_value=SimpleNamespace(upsert_status=MagicMock())
    )
    route = OriginRoute(
        platform="telegram",
        chat_id="-100123",
        thread_id="9",
        session_key="agent:main:telegram:group:-100123:42",
        profile="default",
        chat_type="group",
        status_metadata={"thread_id": "9"},
    )

    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        with pytest.raises(RuntimeError, match="not accepted"):
            await runner._handle_status_ingress_event(route, _event())


@pytest.mark.asyncio
async def test_external_status_event_reports_publication_failure():
    runner = object.__new__(GatewayRunner)
    runner._adapter_for_source = MagicMock(
        return_value=SimpleNamespace(upsert_status=MagicMock())
    )
    route = OriginRoute(
        platform="telegram",
        chat_id="-100123",
        thread_id="9",
        session_key="agent:main:telegram:group:-100123:42",
        profile="default",
        chat_type="group",
        status_metadata={"thread_id": "9"},
    )

    def reject(*_args, **kwargs):
        kwargs["context"].metadata["_status_ingress_ack"].set_result(False)
        return [object()]

    with patch("hermes_cli.plugins.invoke_hook", side_effect=reject):
        with pytest.raises(RuntimeError, match="publication failed"):
            await runner._handle_status_ingress_event(route, _event())


@pytest.mark.asyncio
async def test_status_ingress_is_disabled_without_breaking_windows_gateway_startup():
    runner = object.__new__(GatewayRunner)
    ingress = SimpleNamespace(start=MagicMock())
    runner.__dict__["_status_ingress"] = ingress

    with patch("gateway.run.sys.platform", "win32"):
        await runner._start_status_ingress()

    ingress.start.assert_not_called()


@pytest.mark.asyncio
async def test_status_ingress_start_failure_degrades_without_aborting_gateway():
    runner = object.__new__(GatewayRunner)
    ingress = SimpleNamespace(
        start=AsyncMock(side_effect=OSError("socket unavailable")),
        stop=AsyncMock(),
    )
    runner.__dict__["_status_ingress"] = ingress

    await runner._start_status_ingress()

    ingress.stop.assert_awaited_once()
    assert runner._status_ingress is None
