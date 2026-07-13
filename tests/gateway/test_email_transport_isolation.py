import os
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.email.adapter import EmailAdapter, _extract_attachments


def make_adapter():
    with patch.dict(os.environ, {
        "EMAIL_ADDRESS": "hermes@test.com",
        "EMAIL_PASSWORD": "secret",
        "EMAIL_IMAP_HOST": "imap.test.com",
        "EMAIL_SMTP_HOST": "smtp.test.com",
    }):
        return EmailAdapter(PlatformConfig(enabled=True))


def test_email_declares_request_scoped_delivery_capabilities():
    adapter = make_adapter()
    assert adapter.supports_async_delivery is False
    assert adapter.supports_unsolicited_delivery is False


@pytest.mark.asyncio
async def test_email_rejects_send_without_explicit_allowed_purpose():
    adapter = make_adapter()
    adapter._send_email = MagicMock()
    result = await adapter.send("alex@test.com", "gateway online")
    assert result.success is False
    assert "purpose" in result.error.lower()
    adapter._send_email.assert_not_called()


@pytest.mark.asyncio
async def test_email_direct_reply_requires_and_uses_trigger_message_id():
    adapter = make_adapter()
    adapter._send_email = MagicMock(return_value="<out@test.com>")
    missing = await adapter.send(
        "alex@test.com", "reply", metadata={"email_purpose": "direct_reply"}
    )
    sent = await adapter.send(
        "alex@test.com",
        "reply",
        reply_to="<in@test.com>",
        metadata={"email_purpose": "direct_reply"},
    )
    assert missing.success is False
    assert sent.success is True
    adapter._send_email.assert_called_once_with(
        "alex@test.com", "reply", "<in@test.com>", None, "direct_reply", None
    )


@pytest.mark.asyncio
async def test_request_bound_retry_path_marks_email_as_direct_reply():
    adapter = make_adapter()
    adapter.send = AsyncMock(
        return_value=__import__("gateway.platforms.base", fromlist=["SendResult"]).SendResult(success=True)
    )

    await adapter._send_with_retry(
        "alex@test.com",
        "command response",
        reply_to="<in@test.com>",
        metadata={"email_references": "<root@test.com> <in@test.com>"},
    )

    sent_metadata = adapter.send.await_args.kwargs["metadata"]
    assert sent_metadata["email_purpose"] == "direct_reply"
    assert sent_metadata["email_references"] == "<root@test.com> <in@test.com>"


@pytest.mark.asyncio
async def test_explicit_review_packet_starts_fresh_even_with_stale_sender_cache():
    adapter = make_adapter()
    adapter._thread_context["alex@test.com"] = {
        "subject": "Unrelated old thread",
        "message_id": "<old@test.com>",
    }
    adapter._send_email = MagicMock(return_value="<review@test.com>")
    result = await adapter.send(
        "alex@test.com",
        "review packet",
        metadata={
            "email_purpose": "explicit_review_packet",
            "email_subject": "Sam review receipt",
        },
    )
    assert result.success is True
    adapter._send_email.assert_called_once_with(
        "alex@test.com", "review packet", None, "Sam review receipt", "explicit_review_packet", None
    )


def test_thread_headers_do_not_implicitly_fall_back_to_latest_sender_thread():
    adapter = make_adapter()
    adapter._thread_context["alex@test.com"] = {
        "subject": "Old request",
        "message_id": "<old@test.com>",
    }
    _, headers = adapter._build_thread_headers("alex@test.com", None)
    assert "In-Reply-To" not in headers
    assert "References" not in headers
    assert adapter._build_reply_subject("alex@test.com", None) == "Hermes Agent"
    assert adapter._build_reply_subject("alex@test.com", "<old@test.com>") == "Re: Old request"


def test_exact_message_id_context_survives_newer_email_from_same_sender():
    adapter = make_adapter()
    adapter._thread_context["alex@test.com"] = {
        "subject": "Newest request",
        "message_id": "<new@test.com>",
    }
    adapter._thread_context_by_message_id["<old@test.com>"] = {
        "subject": "Old request",
        "references": "<root@test.com>",
    }

    _, headers = adapter._build_thread_headers("alex@test.com", "<old@test.com>")

    assert adapter._build_reply_subject("alex@test.com", "<old@test.com>") == "Re: Old request"
    assert headers["In-Reply-To"] == "<old@test.com>"
    assert headers["References"] == "<root@test.com> <old@test.com>"


def test_sam_attachment_intake_collects_metadata_without_caching_payload():
    msg = MIMEMultipart()
    attachment = MIMEApplication(b"untrusted bytes", _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename="private.pdf")
    msg.attach(attachment)
    with patch("plugins.platforms.email.adapter.cache_document_from_bytes") as cache:
        result = _extract_attachments(msg, metadata_only=True)
    cache.assert_not_called()
    assert result[0]["filename"] == "private.pdf"
    assert result[0]["type"] == "document"
    assert result[0]["media_type"] == "application/pdf"
    assert result[0]["size"] > 0


@pytest.mark.asyncio
async def test_authenticated_sam_is_consumed_by_restricted_route_before_normal_session():
    adapter = make_adapter()
    adapter._sam_route = AsyncMock()
    adapter._sam_route.handle.return_value = True
    adapter.set_message_handler(AsyncMock())
    msg_data = {
        "sender_addr": "sammyphillips19@gmail.com",
        "sender_name": "Sam",
        "sender_authenticated": True,
        "subject": "Hello",
        "message_id": "<sam@test.com>",
        "in_reply_to": "",
        "references": "",
        "body": "Hi",
        "attachments": [],
        "date": "",
    }
    await adapter._dispatch_message(msg_data)
    adapter._sam_route.handle.assert_awaited_once_with(msg_data)
    adapter._message_handler.assert_not_awaited()
    assert "sammyphillips19@gmail.com" not in adapter._thread_context


@pytest.mark.asyncio
async def test_alex_receipt_approval_command_is_consumed_without_normal_session():
    adapter = make_adapter()
    restricted = MagicMock()
    restricted.handle = AsyncMock(return_value=False)
    restricted.approve = AsyncMock(return_value={"status": "sent"})
    adapter._sam_route = restricted
    adapter._message_handler = AsyncMock()
    adapter.send = AsyncMock(return_value=__import__("gateway.platforms.base", fromlist=["SendResult"]).SendResult(success=True))
    data = {
        "sender_addr": "alexcolodner@gmail.com",
        "sender_name": "Alex",
        "sender_authenticated": True,
        "subject": "Sam receipt",
        "message_id": "<alex@test.com>",
        "in_reply_to": "",
        "references": "",
        "body": "SAM APPROVE sam-123",
        "attachments": [],
        "date": "",
    }

    await adapter._dispatch_message(data)

    restricted.approve.assert_awaited_once_with("sam-123")
    adapter._message_handler.assert_not_called()
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.kwargs["metadata"]["email_purpose"] == "explicit_notification"


@pytest.mark.asyncio
async def test_sam_drafter_constructs_no_tools_no_memory_agent():
    adapter = make_adapter()
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_conversation(self, *_args, **_kwargs):
            return {
                "final_response": '{"outcome":"REVIEW_REQUIRED","reply":"Draft","reason":"Review"}'
            }

        def close(self):
            return None

    with patch("run_agent.AIAgent", FakeAgent):
        result = await adapter._draft_sam_restricted({"body": "untrusted"})

    assert result["outcome"] == "REVIEW_REQUIRED"
    assert captured["enabled_toolsets"] == []
    assert captured["skip_memory"] is True
    assert captured["skip_context_files"] is True
    assert captured["load_soul_identity"] is False
    assert captured["max_iterations"] == 1
