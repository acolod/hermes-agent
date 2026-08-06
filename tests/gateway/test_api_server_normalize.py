"""Tests for _normalize_chat_content in the API server adapter."""

from gateway.platforms import api_server
from types import SimpleNamespace

from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.api_server import _normalize_chat_content


class TestNormalizeChatContent:
    """Content normalization converts array-based content parts to plain text."""

    def test_none_returns_empty_string(self):
        assert _normalize_chat_content(None) == ""

    def test_plain_string_returned_as_is(self):
        assert _normalize_chat_content("hello world") == "hello world"


    def test_text_content_part(self):
        content = [{"type": "text", "text": "hello"}]
        assert _normalize_chat_content(content) == "hello"

    def test_input_text_content_part(self):
        content = [{"type": "input_text", "text": "user input"}]
        assert _normalize_chat_content(content) == "user input"

    def test_output_text_content_part(self):
        content = [{"type": "output_text", "text": "assistant output"}]
        assert _normalize_chat_content(content) == "assistant output"


    def test_empty_text_parts_filtered(self):
        content = [
            {"type": "text", "text": ""},
            {"type": "text", "text": "actual"},
            {"type": "text", "text": ""},
        ]
        assert _normalize_chat_content(content) == "actual"


    def test_empty_list_returns_empty(self):
        assert _normalize_chat_content([]) == ""

    def test_many_small_parts_normalize_without_quadratic_rescan(self, monkeypatch):
        """Large content arrays should normalize in linear time."""
        content = [{"type": "text", "text": "x"} for _ in range(1000)]
        sum_calls = 0

        def counting_sum(values):
            nonlocal sum_calls
            sum_calls += 1
            return sum(values)

        monkeypatch.setattr(api_server, "sum", counting_sum, raising=False)
        result = _normalize_chat_content(content)

        assert result.count("x") == 1000
        assert sum_calls == 0

    def test_raw_content_block_repr_extracts_visible_text(self):
        raw = "[SimpleNamespace(type='output_text', text='Visible answer', annotations=[])]"
        assert _normalize_chat_content(raw) == "Visible answer"

    def test_known_leak_payload_json_is_suppressed(self):
        raw = (
            '{"events": [{"created_at": "2026-01-01T00:00:00Z", "run_id": "run_123"}], '
            '"worker_context": "internal", "summary": "debug"}'
        )
        assert _normalize_chat_content(raw) == ""

    def test_message_response_normalizes_structured_content_and_reasoning(self):
        payload = APIServerAdapter._message_response(
            {
                "id": "msg_1",
                "role": "assistant",
                "content": [
                    SimpleNamespace(type="output_text", text="Hello from content"),
                ],
                "reasoning": "[SimpleNamespace(type='summary_text', text='Think step', annotations=[])]",
                "reasoning_content": [{"type": "output_text", "text": "Shown reasoning"}],
            }
        )
        assert payload["content"] == "Hello from content"
        assert payload["reasoning"] == "Think step"
        assert payload["reasoning_content"] == "Shown reasoning"

    def test_extract_output_items_final_message_normalizes_leak_repr(self):
        items = APIServerAdapter._extract_output_items(
            {
                "final_response": "[SimpleNamespace(type='output_text', text='Visible answer', annotations=[])]",
            }
        )
        assert items[-1]["content"][0]["text"] == "Visible answer"