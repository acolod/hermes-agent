"""Tests for TelegramAdapter.send_or_update_status (issue #30045).

The status-update path must:
  1. Send a fresh message on the first call for a (chat_id, status_key) pair.
  2. Edit that same message on subsequent calls with the same key.
  3. Fall back to sending fresh when the cached message edit fails.
  4. Keep distinct keys independent (no cross-talk).
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult


def _install_fake_telegram(monkeypatch):
    """Stub the python-telegram-bot package so TelegramAdapter can be imported."""
    fake_telegram = types.ModuleType("telegram")
    fake_telegram.Update = SimpleNamespace(ALL_TYPES=())
    fake_telegram.Bot = object
    fake_telegram.Message = object
    fake_telegram.InlineKeyboardButton = object
    fake_telegram.InlineKeyboardMarkup = object

    fake_error = types.ModuleType("telegram.error")
    fake_error.NetworkError = type("NetworkError", (Exception,), {})
    fake_error.BadRequest = type("BadRequest", (Exception,), {})
    fake_error.TimedOut = type("TimedOut", (Exception,), {})
    fake_telegram.error = fake_error

    fake_constants = types.ModuleType("telegram.constants")
    fake_constants.ParseMode = SimpleNamespace(MARKDOWN_V2="MarkdownV2")
    fake_constants.ChatType = SimpleNamespace(
        GROUP="group", SUPERGROUP="supergroup",
        CHANNEL="channel", PRIVATE="private",
    )
    fake_telegram.constants = fake_constants

    fake_ext = types.ModuleType("telegram.ext")
    fake_ext.Application = object
    fake_ext.CommandHandler = object
    fake_ext.CallbackQueryHandler = object
    fake_ext.MessageHandler = object
    fake_ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    fake_ext.filters = object

    fake_request = types.ModuleType("telegram.request")
    fake_request.HTTPXRequest = object

    monkeypatch.setitem(sys.modules, "telegram", fake_telegram)
    monkeypatch.setitem(sys.modules, "telegram.error", fake_error)
    monkeypatch.setitem(sys.modules, "telegram.constants", fake_constants)
    monkeypatch.setitem(sys.modules, "telegram.ext", fake_ext)
    monkeypatch.setitem(sys.modules, "telegram.request", fake_request)


@pytest.fixture
def adapter(monkeypatch):
    _install_fake_telegram(monkeypatch)
    from plugins.platforms.telegram.adapter import TelegramAdapter

    a = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    a._bot = MagicMock()
    # Patch send / edit_message so tests can drive them directly.
    a.send = AsyncMock()
    a.edit_message = AsyncMock()
    return a


@pytest.mark.asyncio
async def test_first_call_sends_and_caches_message_id(adapter):
    """First call for a (chat, key) pair must send and remember the id."""
    adapter.send.return_value = SendResult(success=True, message_id="100")

    result = await adapter.send_or_update_status("chat-1", "lifecycle", "starting")

    assert result.success is True
    assert result.message_id == "100"
    adapter.send.assert_awaited_once()
    adapter.edit_message.assert_not_awaited()
    assert adapter._status_message_ids[("chat-1", "lifecycle")] == "100"


@pytest.mark.asyncio
async def test_distinct_status_keys_do_not_collide(adapter):
    """A different status_key gets its own message; the original isn't touched."""
    adapter.send.side_effect = [
        SendResult(success=True, message_id="100"),
        SendResult(success=True, message_id="200"),
    ]

    await adapter.send_or_update_status("chat-1", "lifecycle", "ctx pressure")
    await adapter.send_or_update_status("chat-1", "model-switch", "switched to opus")

    assert adapter.send.await_count == 2
    adapter.edit_message.assert_not_awaited()
    assert adapter._status_message_ids[("chat-1", "lifecycle")] == "100"
    assert adapter._status_message_ids[("chat-1", "model-switch")] == "200"


@pytest.mark.asyncio
async def test_distinct_chat_ids_do_not_collide(adapter):
    """Same status_key in different chats must not edit each other's messages."""
    adapter.send.side_effect = [
        SendResult(success=True, message_id="100"),
        SendResult(success=True, message_id="200"),
    ]

    await adapter.send_or_update_status("chat-1", "lifecycle", "first")
    await adapter.send_or_update_status("chat-2", "lifecycle", "second")

    assert adapter.send.await_count == 2
    adapter.edit_message.assert_not_awaited()
    assert adapter._status_message_ids[("chat-1", "lifecycle")] == "100"
    assert adapter._status_message_ids[("chat-2", "lifecycle")] == "200"


@pytest.mark.asyncio
async def test_distinct_thread_ids_do_not_collide(adapter):
    """The same card key in separate topics must own separate messages."""
    adapter.send.side_effect = [
        SendResult(success=True, message_id="100"),
        SendResult(success=True, message_id="200"),
    ]

    await adapter.send_or_update_status(
        "chat-1", "taskcard", "topic one", metadata={"thread_id": "thread-1"}
    )
    await adapter.send_or_update_status(
        "chat-1", "taskcard", "topic two", metadata={"thread_id": "thread-2"}
    )

    assert adapter.send.await_count == 2
    adapter.edit_message.assert_not_awaited()
    assert adapter._status_message_ids[("chat-1", "thread-1", "taskcard")] == "100"
    assert adapter._status_message_ids[("chat-1", "thread-2", "taskcard")] == "200"


@pytest.mark.asyncio
async def test_status_message_cache_is_bounded(adapter):
    """Unique status keys evict the oldest cached Telegram message binding."""
    adapter._status_message_cache_max = 2
    adapter.send.side_effect = [
        SendResult(success=True, message_id="100"),
        SendResult(success=True, message_id="200"),
        SendResult(success=True, message_id="300"),
    ]

    await adapter.send_or_update_status("chat-1", "status-1", "one")
    await adapter.send_or_update_status("chat-1", "status-2", "two")
    await adapter.send_or_update_status("chat-1", "status-3", "three")

    assert len(adapter._status_message_ids) == 2
    assert ("chat-1", "status-1") not in adapter._status_message_ids


@pytest.mark.asyncio
async def test_persisted_status_message_id_recovers_edit_after_restart(adapter, caplog):
    """A persisted binding edits the prior bubble when the cache starts empty."""
    caplog.set_level("INFO")
    adapter.edit_message.return_value = SendResult(success=True, message_id="15438")

    result = await adapter.send_or_update_status(
        "chat-1",
        "taskcard",
        "recovered",
        metadata={
            "thread_id": "thread-1",
            "status_message_id": "15438",
        },
    )

    assert result.success is True
    adapter.send.assert_not_awaited()
    adapter.edit_message.assert_awaited_once()
    assert adapter.edit_message.call_args.args[1] == "15438"
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Telegram status binding source=persisted" in message
        and "message_id=15438" in message
        for message in messages
    )
    assert any(
        "Telegram status edit begin" in message and "message_id=15438" in message
        for message in messages
    )
    assert any(
        "Telegram status edit result" in message
        and "success=True" in message
        and "disposition=success" in message
        for message in messages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned_chat", "returned_message", "returned_text", "success", "reason"),
    [
        ("-5141963563", "15438", "Task Card rev 1", True, "validated"),
        ("-1", "15438", "Task Card rev 1", False, "chat_mismatch"),
        ("-5141963563", "999", "Task Card rev 1", False, "message_mismatch"),
        ("-5141963563", "15438", "stale rev 3", False, "text_mismatch"),
    ],
)
async def test_edit_message_validates_returned_message(
    adapter,
    caplog,
    returned_chat,
    returned_message,
    returned_text,
    success,
    reason,
):
    """A Telegram edit is successful only when its returned Message matches."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    caplog.set_level("INFO")
    adapter.edit_message = TelegramAdapter.edit_message.__get__(adapter, TelegramAdapter)
    adapter._bot.edit_message_text = AsyncMock(
        return_value=SimpleNamespace(
            chat=SimpleNamespace(id=int(returned_chat)),
            message_id=int(returned_message),
            text=returned_text,
        )
    )

    result = await adapter.edit_message(
        "-5141963563",
        "15438",
        "Task Card rev 1",
        finalize=True,
        metadata={"validate_edit_response": True},
    )

    assert result.success is success
    assert result.message_id == "15438"
    assert result.raw_response["reason"] == reason
    assert result.raw_response["expected_chat_id"] == "-5141963563"
    assert result.raw_response["returned_chat_id"] == returned_chat
    assert result.raw_response["expected_message_id"] == "15438"
    assert result.raw_response["returned_message_id"] == returned_message
    assert len(result.raw_response["expected_text_hash"]) == 64
    assert len(result.raw_response["returned_text_hash"]) == 64
    assert any(
        "Telegram edit response validation" in record.getMessage()
        and f"disposition={reason}" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_markdown_v2_task_card_edit_validates_rendered_visible_text(adapter):
    """Task-card MarkdownV2 edits validate the text Telegram visibly returns."""
    from plugins.platforms.telegram.adapter import (
        TelegramAdapter,
        _canonical_mdv2_visible_text,
    )

    content = (
        "✅ COMPLETED\n\n"
        "✅ 1. ~~*Task name*~~\n"
        "── OUTCOME ──\n"
        "Result: *Fixed*.\n"
        "Next: review (today)!"
    )
    rendered_text = (
        "✅ COMPLETED\n\n"
        "✅ 1. Task name\n"
        "── OUTCOME ──\n"
        "Result: Fixed.\n"
        "Next: review (today)!"
    )
    adapter.edit_message = TelegramAdapter.edit_message.__get__(adapter, TelegramAdapter)
    adapter._bot.edit_message_text = AsyncMock(
        return_value=SimpleNamespace(
            chat=SimpleNamespace(id=-5141963563),
            message_id=15438,
            text=rendered_text,
        )
    )

    result = await adapter.edit_message(
        "-5141963563",
        "15438",
        content,
        finalize=True,
        metadata={"validate_edit_response": True},
    )

    assert result.success is True
    assert result.raw_response["reason"] == "validated"
    assert adapter._bot.edit_message_text.call_args.kwargs["text"] != content
    assert adapter._bot.edit_message_text.call_args.kwargs["parse_mode"] == "MarkdownV2"
    literal_source = r"Result: *Fixed*\."
    assert _canonical_mdv2_visible_text(adapter.format_message(literal_source)) == r"Result: Fixed\."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returned_text", "success"),
    [
        ("Result: received then working", True),
        ("Result: picked_up then working", False),
        ("Result: received then blocked", False),
    ],
)
async def test_markdown_v2_edit_validates_visible_inline_code_and_link_label(
    adapter,
    returned_text,
    success,
):
    """Inline code and links compare against the visible Telegram text only."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter.edit_message = TelegramAdapter.edit_message.__get__(adapter, TelegramAdapter)
    adapter._bot.edit_message_text = AsyncMock(
        return_value=SimpleNamespace(
            chat=SimpleNamespace(id=-5141963563),
            message_id=15438,
            text=returned_text,
        )
    )

    result = await adapter.edit_message(
        "-5141963563",
        "15438",
        "Result: `received` then [working](https://example.test/status)",
        finalize=True,
        metadata={"validate_edit_response": True},
    )

    assert result.success is success
    assert result.raw_response["reason"] == ("validated" if success else "text_mismatch")


def test_markdown_v2_canonicalizer_preserves_fenced_code_body_linebreaks(adapter):
    """Fenced-code delimiters and language headers are not visible Telegram text."""
    from plugins.platforms.telegram.adapter import _canonical_mdv2_visible_text

    source = "Result:\n```text\nreceived\npicked_up\nworking\n```"

    assert _canonical_mdv2_visible_text(adapter.format_message(source)) == (
        "Result:\nreceived\npicked_up\nworking"
    )


@pytest.mark.asyncio
async def test_unverified_edit_does_not_create_replacement(adapter):
    """A response mismatch must preserve the binding instead of sending anew."""
    adapter.edit_message.return_value = SendResult(
        success=False,
        message_id="15438",
        error="edit response text mismatch",
        error_kind="edit_response_mismatch",
    )

    result = await adapter.send_or_update_status(
        "-5141963563",
        "taskcard",
        "Task Card rev 1",
        metadata={"status_message_id": "15438"},
    )

    assert result.success is False
    assert result.message_id == "15438"
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_markdown_edit_does_not_require_task_card_validation(adapter):
    """Strict response matching must not change ordinary Markdown delivery."""
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter.edit_message = TelegramAdapter.edit_message.__get__(adapter, TelegramAdapter)
    adapter._bot.edit_message_text = AsyncMock(
        return_value=SimpleNamespace(
            chat=SimpleNamespace(id=-5141963563),
            message_id=15438,
            text="bold",
        )
    )

    result = await adapter.edit_message(
        "-5141963563",
        "15438",
        "**bold**",
        finalize=True,
    )

    assert result.success is True
    assert result.raw_response is None


@pytest.mark.asyncio
async def test_status_update_acknowledges_normalized_todo_edit_in_place(adapter):
    """A Todo edit falls back exactly and succeeds instead of dispatch_failed."""
    from plugins.platforms.telegram.adapter import TelegramAdapter, _strip_mdv2

    content = "✅ 1. ~~*Task name*~~\n── OUTCOME ──\nResult: Fixed."
    plain = _strip_mdv2(content)
    adapter.edit_message = TelegramAdapter.edit_message.__get__(adapter, TelegramAdapter)
    adapter._status_message_ids[("-5141963563", "taskcard")] = "15438"
    adapter._bot.edit_message_text = AsyncMock(
        side_effect=[
            Exception("MarkdownV2 parse failed"),
            SimpleNamespace(
                chat=SimpleNamespace(id=-5141963563),
                message_id=15438,
                text=plain,
            ),
        ]
    )

    result = await adapter.send_or_update_status(
        "-5141963563",
        "taskcard",
        content,
        metadata={"validate_edit_response": True, "preserve_status_message_id": True},
    )

    assert result.success is True
    assert result.message_id == "15438"
    assert result.error_kind is None
    adapter.send.assert_not_awaited()
    assert adapter._bot.edit_message_text.await_args_list[-1].kwargs == {
        "chat_id": -5141963563,
        "message_id": 15438,
        "text": plain,
    }


@pytest.mark.asyncio
async def test_preserved_status_message_retries_same_id_without_fresh_send(adapter):
    """A preserved binding survives a transient edit failure and later succeeds."""
    adapter.edit_message.side_effect = [
        SendResult(
            success=False,
            message_id="15499",
            error="flood control",
            retry_after=2.0,
            error_kind="rate_limited",
        ),
        SendResult(success=True, message_id="15499"),
    ]
    metadata = {
        "status_message_id": "15499",
        "preserve_status_message_id": True,
    }

    failed = await adapter.send_or_update_status(
        "-5141963563", "taskcard", "running", metadata=metadata,
    )
    succeeded = await adapter.send_or_update_status(
        "-5141963563", "taskcard", "completed", metadata=metadata,
    )

    assert failed.success is False
    assert failed.message_id == "15499"
    assert failed.retry_after == 2.0
    assert succeeded.success is True
    assert [call.args[1] for call in adapter.edit_message.await_args_list] == ["15499", "15499"]
    assert adapter._status_message_ids[("-5141963563", "taskcard")] == "15499"
    adapter.send.assert_not_awaited()