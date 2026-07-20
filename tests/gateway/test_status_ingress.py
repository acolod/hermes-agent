import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gateway.status_ingress import (
    GatewayStatusIngress,
    OriginRoute,
    StatusOriginRegistry,
    validate_status_event,
)


def _event(**overrides):
    payload = {
        "activity_kind": "relay",
        "activity_id": "relay:task-123",
        "generation": "gen-123",
        "revision": 1,
        "phase": "started",
        "status": "running",
        "summary": "Relay task started",
        "metadata": {"relay_path": "task"},
    }
    payload.update(overrides)
    return payload


def _route():
    return OriginRoute(
        platform="telegram",
        chat_id="-100123",
        thread_id="9",
        session_key="agent:main:telegram:group:-100123:42",
        profile="default",
        chat_type="group",
        status_metadata={"thread_id": "9", "reply_to_message_id": "42"},
    )


def test_origin_registry_returns_opaque_stable_handle_and_recovers_after_restart(tmp_path):
    registry = StatusOriginRegistry(tmp_path)

    handle = registry.register(_route())
    restarted = StatusOriginRegistry(tmp_path)

    assert handle.startswith("origin_")
    assert "telegram" not in handle
    assert "100123" not in handle
    assert restarted.register(_route()) == handle
    assert restarted.resolve(handle) == _route()
    assert (tmp_path / "origins.json").stat().st_mode & 0o777 == 0o600

    updated = OriginRoute(
        **{**_route().__dict__, "status_metadata": {"reply_to_message_id": "99"}}
    )
    assert restarted.register(updated) == handle
    assert StatusOriginRegistry(tmp_path).resolve(handle) == updated


def test_origin_registry_persistence_is_bounded(tmp_path):
    registry = StatusOriginRegistry(tmp_path)
    handles = []
    for index in range(300):
        handles.append(
            registry.register(
                OriginRoute(platform="telegram", chat_id=str(index), session_key=f"session-{index}")
            )
        )

    restarted = StatusOriginRegistry(tmp_path)
    persisted = json.loads((tmp_path / "origins.json").read_text(encoding="utf-8"))

    assert len(persisted["origins"]) == 256
    assert restarted.resolve(handles[-1]) is not None
    assert sum(restarted.resolve(item) is not None for item in handles) == 256


def test_status_event_rejects_caller_selected_routing_even_inside_metadata():
    for forbidden in (
        "chat_id",
        "thread_id",
        "session_key",
        "workspace_id",
        "reply_to_message_id",
        "status_message_id",
        "telegram_message_id",
    ):
        with pytest.raises(ValueError, match="unsupported field"):
            validate_status_event({**_event(), forbidden: "caller-selected"})
        with pytest.raises(ValueError, match="forbidden routing key"):
            validate_status_event({**_event(), "metadata": {forbidden: "smuggled"}})
        with pytest.raises(ValueError, match="forbidden routing key"):
            validate_status_event(
                {**_event(), "metadata": {"nested": [{forbidden: "smuggled"}]}}
            )


def test_status_event_requires_bounded_monotonic_identity_and_metadata():
    assert validate_status_event(_event())["revision"] == 1

    with pytest.raises(ValueError, match="revision"):
        validate_status_event(_event(revision=0))
    with pytest.raises(ValueError, match="metadata"):
        validate_status_event(_event(metadata={"x": "y" * 5000}))
    with pytest.raises(ValueError, match="activity_id"):
        validate_status_event(_event(activity_id="x" * 129))


def test_status_event_accepts_a_bounded_full_task_snapshot_and_preserves_omission():
    task_items = [
        {"id": "inspect", "content": "Inspect the ingress", "status": "completed"},
        {"id": "forward", "content": "Forward the snapshot", "status": "in_progress"},
    ]

    supplied = validate_status_event(_event(task_items=task_items))
    omitted = validate_status_event(_event())

    assert supplied["task_items"] == task_items
    assert "task_items" not in omitted


@pytest.mark.parametrize(
    "task_items",
    [
        [],
        [{"id": "duplicate", "content": "One", "status": "pending"}, {"id": "duplicate", "content": "Two", "status": "completed"}],
        [{"id": "one", "content": "One", "status": "unknown"}],
        [{"id": "one", "content": "One", "status": " pending "}],
        [{"id": "one", "content": "One", "status": "pending", "extra": True}],
        [{"id": "x" * 129, "content": "One", "status": "pending"}],
        [{"id": "one", "content": "x" * 161, "status": "pending"}],
        [{"id": "one", "content": "One", "status": "pending"}] * 17,
    ],
)
def test_status_event_rejects_malformed_task_snapshots(task_items):
    with pytest.raises(ValueError, match="task_items|id|content"):
        validate_status_event(_event(task_items=task_items))


async def _request(socket_path: Path, payload: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(json.dumps(payload).encode("utf-8") + b"\n")
    await writer.drain()
    response = json.loads((await reader.readline()).decode("utf-8"))
    writer.close()
    await writer.wait_closed()
    return response


@pytest.mark.asyncio
async def test_unix_ingress_is_authenticated_fail_closed_and_resolves_origin(tmp_path):
    received = []

    async def on_event(route, event):
        received.append((route, event, asyncio.get_running_loop()))
        return {"accepted": True, "revision": event["revision"]}

    ingress = GatewayStatusIngress(tmp_path, on_event=on_event)
    handle = ingress.register_origin(_route())
    await ingress.start()
    try:
        token = (tmp_path / "token").read_text().strip()
        socket_path = tmp_path / "status.sock"

        unauthenticated = await _request(
            socket_path,
            {"token": "wrong", "origin_handle": handle, "event": _event()},
        )
        unknown = await _request(
            socket_path,
            {"token": token, "origin_handle": "origin_unknown", "event": _event()},
        )
        accepted = await _request(
            socket_path,
            {"token": token, "origin_handle": handle, "event": _event()},
        )
        health = await _request(
            socket_path,
            {"token": token, "origin_handle": handle, "operation": "health"},
        )

        assert unauthenticated == {"ok": False, "error": "unauthorized"}
        assert unknown == {"ok": False, "error": "unknown_origin_handle"}
        assert accepted == {"ok": True, "result": {"accepted": True, "revision": 1}}
        assert health == {"ok": True, "result": {"healthy": True}}
        assert received == [(_route(), validate_status_event(_event()), asyncio.get_running_loop())]
        assert socket_path.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600
    finally:
        await ingress.stop()

    assert not (tmp_path / "status.sock").exists()


@pytest.mark.asyncio
async def test_long_socket_path_uses_private_owned_runtime_directory(tmp_path):
    root = tmp_path / ("long-status-ingress-" + "x" * 90)
    ingress = GatewayStatusIngress(root, on_event=AsyncMock())
    await ingress.start()
    try:
        assert ingress.socket_path.parent != Path(tempfile.gettempdir())
        assert ingress.socket_path.parent.stat().st_uid == os.getuid()
        assert ingress.socket_path.parent.stat().st_mode & 0o777 == 0o700
        assert ingress.socket_path.stat().st_mode & 0o777 == 0o600
    finally:
        await ingress.stop()


@pytest.mark.asyncio
async def test_ingress_rejects_oversized_or_malformed_requests_without_callback(tmp_path):
    received = []
    ingress = GatewayStatusIngress(tmp_path, on_event=lambda route, event: received.append((route, event)))
    ingress.register_origin(_route())
    await ingress.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(tmp_path / "status.sock"))
        writer.write(b"{" + b"x" * 70000 + b"}\n")
        await writer.drain()
        response = json.loads((await reader.readline()).decode("utf-8"))
        writer.close()
        await writer.wait_closed()

        assert response == {"ok": False, "error": "request_too_large"}
        assert received == []
    finally:
        await ingress.stop()
