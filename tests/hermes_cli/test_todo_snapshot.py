import json
import os
from pathlib import Path

from hermes_cli.todo_snapshot import TodoSnapshotWriter


def _todo_result(items):
    return {"name": "todo", "result": json.dumps({"todos": items})}


def test_writer_persists_bounded_normalized_current_turn_snapshot(tmp_path):
    path = tmp_path / "private" / "todo-snapshot.json"
    writer = TodoSnapshotWriter(path)
    items = [
        {"id": "active", "content": "A" * 200, "status": "in_progress"},
        {"id": "done", "content": "Done", "status": "completed"},
        {"id": "bad-status", "content": "Ignore", "status": "blocked"},
        {"id": "", "content": "Ignore", "status": "pending"},
    ] + [
        {"id": f"later-{index}", "content": "Later", "status": "pending"}
        for index in range(20)
    ]

    assert writer.observe([], [_todo_result(items)]) is True

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload) == 16
    assert payload[0] == {"id": "active", "content": "A" * 160, "status": "in_progress"}
    assert payload[1] == {"id": "done", "content": "Done", "status": "completed"}
    assert all(item["id"] != "bad-status" for item in payload)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_writer_ignores_malformed_results_and_dedupes_identical_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "todo-snapshot.json"
    writer = TodoSnapshotWriter(path)
    tools = [_todo_result([{"id": "one", "content": "One", "status": "pending"}])]

    assert writer.observe([], tools) is True
    replace_calls = 0
    real_replace = os.replace

    def counting_replace(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", counting_replace)
    assert writer.observe([], tools) is False
    assert replace_calls == 0
    assert writer.observe([], [{"name": "todo", "result": "not json"}]) is False
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"id": "one", "content": "One", "status": "pending"}
    ]


def test_writer_uses_last_valid_completed_todo_result_from_current_batch(tmp_path):
    writer = TodoSnapshotWriter(tmp_path / "todo-snapshot.json")

    assert writer.observe(
        [],
        [
            _todo_result([{"id": "old", "content": "Old", "status": "pending"}]),
            {"name": "web_search", "result": "ignored"},
            _todo_result([{"id": "current", "content": "Current", "status": "in_progress"}]),
        ],
    ) is True

    assert json.loads((tmp_path / "todo-snapshot.json").read_text(encoding="utf-8")) == [
        {"id": "current", "content": "Current", "status": "in_progress"}
    ]


def test_writer_preserves_prior_snapshot_for_empty_or_all_invalid_results(tmp_path):
    path = tmp_path / "private" / "todo-snapshot.json"
    writer = TodoSnapshotWriter(path)
    prior = [{"id": "prior", "content": "Prior", "status": "pending"}]
    assert writer.observe([], [_todo_result(prior)]) is True

    assert writer.observe([], [_todo_result([])]) is False
    assert json.loads(path.read_text(encoding="utf-8")) == prior

    assert writer.observe([], [_todo_result([{"id": "", "content": "Missing id", "status": "pending"}])]) is False
    assert json.loads(path.read_text(encoding="utf-8")) == prior


def test_writer_refuses_symlinked_target_or_parent_and_preserves_prior_snapshot(tmp_path):
    prior = [{"id": "prior", "content": "Prior", "status": "pending"}]
    candidate = [_todo_result([{"id": "current", "content": "Current", "status": "pending"}])]

    target = tmp_path / "target.json"
    target.write_text(json.dumps(prior), encoding="utf-8")
    linked_target = tmp_path / "linked-target.json"
    linked_target.symlink_to(target)
    assert TodoSnapshotWriter(linked_target).observe([], candidate) is False
    assert json.loads(target.read_text(encoding="utf-8")) == prior

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    parent_target = real_parent / "todo-snapshot.json"
    parent_target.write_text(json.dumps(prior), encoding="utf-8")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    assert TodoSnapshotWriter(linked_parent / "todo-snapshot.json").observe([], candidate) is False
    assert json.loads(parent_target.read_text(encoding="utf-8")) == prior
