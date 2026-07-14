import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram import adapter as telegram_adapter


class ApplicationHandlerStop(Exception):
    pass


telegram_adapter.ApplicationHandlerStop = ApplicationHandlerStop
TelegramAdapter = telegram_adapter.TelegramAdapter


class Bridge:
    def __init__(self, *, setup=None, matched=False, response="✅ Captured as IDEA-0001"):
        self.setup_response = setup
        self.matched = matched
        self.response = response
        self.routed = []

    def setup(self, payload):
        return self.setup_response

    def matches(self, payload):
        return self.matched

    def route(self, payload, downloaded_path=None):
        self.routed.append((payload, downloaded_path))
        return self.response


def update(text="hello"):
    message = SimpleNamespace(
        text=text,
        voice=None,
        audio=None,
        video=None,
        photo=None,
        document=None,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(message=message, effective_message=message, to_dict=lambda: {"update_id": 1})


def adapter(bridge):
    value = TelegramAdapter.__new__(TelegramAdapter)
    value._idea_inbox_bridge = bridge
    value._download_idea_inbox_media = AsyncMock(return_value=None)
    return value


def run(coro):
    return asyncio.run(coro)


def test_setup_stops_before_normal_hermes_routing():
    item = adapter(Bridge(setup="✅ Idea Inbox capture lane configured."))
    event = update("/idea_inbox_setup")
    with pytest.raises(ApplicationHandlerStop):
        run(item._handle_idea_inbox_message(event, None))
    event.message.reply_text.assert_awaited_once_with("✅ Idea Inbox capture lane configured.")


def test_non_lane_falls_through_to_normal_kimi_dm():
    item = adapter(Bridge(matched=False))
    event = update()
    assert run(item._handle_idea_inbox_message(event, None)) is None
    event.message.reply_text.assert_not_awaited()


def test_exact_lane_capture_stops_before_kimi_or_relay():
    bridge = Bridge(matched=True)
    item = adapter(bridge)
    event = update()
    with pytest.raises(ApplicationHandlerStop):
        run(item._handle_idea_inbox_message(event, None))
    assert bridge.routed == [({"update_id": 1}, None)]
    event.message.reply_text.assert_awaited_once_with("✅ Captured as IDEA-0001")
