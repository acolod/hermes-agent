"""Regression coverage for user-visible response normalization."""

from types import SimpleNamespace

from gateway.response_normalization import normalize_visible_text


def test_plain_string_preserves_surrounding_whitespace():
    original = "  ordinary text  "

    assert normalize_visible_text(original) == original


def test_json_looking_example_with_summary_key_is_preserved():
    original = '{"summary":"This is an ordinary JSON example", "value": 1}'

    assert normalize_visible_text(original) == original


def test_json_looking_example_with_unrecognized_type_is_preserved():
    original = '{"type":"example", "text":"ordinary code sample"}'

    assert normalize_visible_text(original) == original


def test_recognized_typed_json_wrapper_extracts_visible_text():
    content = '  {"type":"output_text", "text":"visible reply"}  '

    assert normalize_visible_text(content) == "visible reply"


def test_structured_tool_use_object_is_suppressed():
    content = SimpleNamespace(
        type="tool_use",
        id="tool_1",
        name="web_search",
        input={"query": "x"},
    )

    assert normalize_visible_text(content) == ""


def test_mixed_structured_objects_keep_only_visible_text():
    content = [
        SimpleNamespace(type="output_text", text="Hello"),
        SimpleNamespace(
            type="tool_use",
            id="tool_1",
            name="web_search",
            input={"query": "x"},
        ),
    ]

    assert normalize_visible_text(content) == "Hello"
