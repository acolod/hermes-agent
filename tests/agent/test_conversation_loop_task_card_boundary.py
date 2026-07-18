from agent.conversation_loop import _previous_tools_since_turn


def _tool_call(call_id: str, name: str = "todo") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": "{}"}}


def test_previous_tools_since_turn_excludes_prior_turn_todo_batch():
    messages = [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "tool_calls": [_tool_call("old")]},
        {"role": "tool", "tool_call_id": "old", "content": '{"todos":[{"id":"old"}]}'},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "new task"},
    ]

    assert _previous_tools_since_turn(messages, turn_start=4) == []


def test_previous_tools_since_turn_returns_only_current_turn_todo_batch():
    messages = [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "tool_calls": [_tool_call("old")]},
        {"role": "tool", "tool_call_id": "old", "content": '{"todos":[{"id":"old"}]}'},
        {"role": "user", "content": "new task"},
        {"role": "assistant", "tool_calls": [_tool_call("new")]},
        {"role": "tool", "tool_call_id": "new", "content": '{"todos":[{"id":"new"}]}'},
    ]

    assert _previous_tools_since_turn(messages, turn_start=3) == [
        {"name": "todo", "arguments": "{}", "result": '{"todos":[{"id":"new"}]}'},
    ]


def test_repaired_group_sequence_keeps_current_turn_todo_batch():
    """Synthetic post-repair Telegram group shape; content is deliberately redacted."""
    messages = [
        {"role": "system", "content": "[repair marker]"},
        {"role": "user", "content": "[prior turn]"},
        {"role": "assistant", "content": "[prior answer]"},
        {"role": "user", "content": "[current group prompt]"},
        {"role": "assistant", "tool_calls": [_tool_call("group-todo")]},
        {"role": "tool", "tool_call_id": "group-todo", "content": '{"todos":[{"id":"current"}]}'},
    ]

    assert _previous_tools_since_turn(messages, turn_start=3) == [
        {"name": "todo", "arguments": "{}", "result": '{"todos":[{"id":"current"}]}'},
    ]
