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


def draft_result(
    outcome,
    informational_content,
    reason,
    *,
    verb="",
    obj="",
    target="",
    authority="",
    marker="SAM_RESTRICTED_VALIDATED_V1",
    effect=None,
):
    if effect is None:
        effect = {
            "DIRECT_REPLY": "INFORMATIONAL",
            "SEND_AND_REVIEW_ACTION": "PROTECTED_ACTION",
            "REVIEW_REQUIRED": "PRIVATE_DISCLOSURE",
            "REFUSE": "REFUSAL",
        }[outcome]
    return (
        "{"
        f'"outcome":"{outcome}",'
        f'"informational_content":"{informational_content}",'
        f'"reason":"{reason}",'
        f'"proposed_action_verb":"{verb}",'
        f'"proposed_action_object":"{obj}",'
        f'"proposed_action_target":"{target}",'
        f'"required_authority":"{authority}"'
        "}"
    ), (
        "{"
        '"verdict":"PASS",'
        f'"validation_marker":"{marker}",'
        '"reason":"safe",'
        f'"effect":"{effect}"'
        "}"
    )


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
        "alex@test.com",
        "reply",
        "<in@test.com>",
        None,
        "direct_reply",
        None,
        None,
        None,
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
        "alex@test.com",
        "review packet",
        None,
        "Sam review receipt",
        "explicit_review_packet",
        None,
        None,
        None,
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
    draft_raw, validation_raw = draft_result(
        "REVIEW_REQUIRED",
        "Draft",
        "Review",
    )
    responses = iter([draft_raw, validation_raw])

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_conversation(self, *_args, **_kwargs):
            return {"final_response": next(responses)}

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
    assert captured["provider"] == "openai-codex"
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["fallback_model"] == {
        "provider": "modelrelay",
        "model": "qwen3-32b",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"outcome":"DIRECT_REPLY","informational_content":"Hello","reason":"Safe","proposed_action_verb":"","proposed_action_object":"","proposed_action_target":"","required_authority":""}\n```',
        'Here is the result:\n{"outcome":"DIRECT_REPLY","informational_content":"Hello","reason":"Safe","proposed_action_verb":"","proposed_action_object":"","proposed_action_target":"","required_authority":""}\nDone.',
    ],
)
async def test_sam_drafter_accepts_one_wrapped_json_object(raw):
    adapter = make_adapter()
    _draft_raw, validation_raw = draft_result("DIRECT_REPLY", "Hello", "Safe")
    responses = iter([raw, validation_raw])

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, *_args, **_kwargs):
            return {"final_response": next(responses)}

        def close(self):
            pass

    with patch("run_agent.AIAgent", FakeAgent):
        result = await adapter._draft_sam_restricted({"body": "What is your role?"})

    assert result == {
        "outcome": "DIRECT_REPLY",
        "informational_content": "Hello",
        "reason": "Safe",
        "proposed_action_verb": "",
        "proposed_action_object": "",
        "proposed_action_target": "",
        "required_authority": "",
        "validation_marker": "SAM_RESTRICTED_VALIDATED_V1",
        "validation_reason": "safe",
        "validation_effect": "INFORMATIONAL",
    }


@pytest.mark.asyncio
async def test_sam_drafter_accepts_partial_reply_with_specific_action_request():
    adapter = make_adapter()
    draft_raw, validation_raw = draft_result(
        "SEND_AND_REVIEW_ACTION",
        "I can help plan it; Alex must approve creating it.",
        "Safe discussion plus external action.",
        verb="Create",
        obj="the shared calendar",
        target="for Sam and Alex",
        authority="Alex must explicitly approve creating it.",
    )
    responses = iter([draft_raw, validation_raw])

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, *_args, **_kwargs):
            return {"final_response": next(responses)}

        def close(self):
            pass

    with patch("run_agent.AIAgent", FakeAgent):
        result = await adapter._draft_sam_restricted({"body": "Set up a shared calendar"})

    assert result["outcome"] == "SEND_AND_REVIEW_ACTION"
    assert result["proposed_action_object"] == "the shared calendar"


@pytest.mark.asyncio
async def test_sam_drafter_marks_semantically_unsafe_information_unvalidated():
    adapter = make_adapter()
    draft_raw, _validation_raw = draft_result(
        "DIRECT_REPLY",
        "Access is all set and the provider returned a raw failure.",
        "Candidate answer.",
    )
    failed_validation = (
        '{"verdict":"FAIL","validation_marker":"",'
        '"reason":"candidate contains prohibited status or diagnostics",'
        '"effect":"INFORMATIONAL"}'
    )
    responses = iter([draft_raw, failed_validation])

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, *_args, **_kwargs):
            return {"final_response": next(responses)}

        def close(self):
            pass

    with patch("run_agent.AIAgent", FakeAgent):
        result = await adapter._draft_sam_restricted({"body": "Can you explain this?"})

    assert result["validation_marker"] == ""
    assert result["validation_reason"] == (
        "candidate contains prohibited status or diagnostics"
    )


@pytest.mark.asyncio
async def test_sam_validator_effect_must_align_with_declared_outcome():
    adapter = make_adapter()
    draft_raw, mismatched_validation = draft_result(
        "DIRECT_REPLY",
        "Here are some planning ideas.",
        "Draft called this informational.",
        effect="PROTECTED_ACTION",
    )
    responses = iter([draft_raw, mismatched_validation])

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, *_args, **_kwargs):
            return {"final_response": next(responses)}

        def close(self):
            pass

    with patch("run_agent.AIAgent", FakeAgent):
        result = await adapter._draft_sam_restricted(
            {"body": "Arrange a sitter and confirm it with them."}
        )

    assert result["validation_marker"] == ""
    assert result["validation_effect"] == "PROTECTED_ACTION"
    assert result["validation_reason"] == (
        "validator effect does not match drafted outcome"
    )


@pytest.mark.asyncio
async def test_sam_drafter_repairs_malformed_json_once():
    adapter = make_adapter()
    draft_raw, validation_raw = draft_result("DIRECT_REPLY", "Hello", "Safe")
    responses = iter([
        "{not valid json}",
        draft_raw,
        validation_raw,
    ])
    calls = []

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, prompt, **_kwargs):
            calls.append(prompt)
            return {"final_response": next(responses)}

        def close(self):
            pass

    with patch("run_agent.AIAgent", FakeAgent):
        result = await adapter._draft_sam_restricted({"body": "What is your role?"})

    assert result["outcome"] == "DIRECT_REPLY"
    assert len(calls) == 3
    assert "previous output was invalid" in calls[1].lower()


@pytest.mark.asyncio
async def test_sam_drafter_unrecoverable_malformed_output_fails_closed():
    adapter = make_adapter()
    calls = []

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, prompt, **_kwargs):
            calls.append(prompt)
            return {"final_response": "<html>provider failure</html>"}

        def close(self):
            pass

    with patch("run_agent.AIAgent", FakeAgent):
        with pytest.raises(ValueError, match="structured output"):
            await adapter._draft_sam_restricted({"body": "Tell me something ambiguous"})

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_sam_drafter_rejects_empty_direct_reply():
    adapter = make_adapter()

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, *_args, **_kwargs):
            return {
                "final_response": '{"outcome":"DIRECT_REPLY","informational_content":"","reason":"Safe","proposed_action_verb":"","proposed_action_object":"","proposed_action_target":"","required_authority":""}'
            }

        def close(self):
            pass

    with patch("run_agent.AIAgent", FakeAgent):
        with pytest.raises(ValueError, match="structured output"):
            await adapter._draft_sam_restricted({"body": "What is your role?"})
