import sys
import types


def test_top_level_skills_flag_defaults_to_chat(monkeypatch):
    import hermes_cli.main as main_mod

    captured = {}

    def fake_cmd_chat(args):
        captured["skills"] = args.skills
        captured["command"] = args.command

    monkeypatch.setattr(main_mod, "cmd_chat", fake_cmd_chat)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "-s", "hermes-agent-dev,github-auth"],
    )

    main_mod.main()

    assert captured == {
        "skills": ["hermes-agent-dev,github-auth"],
        "command": None,
    }


def test_chat_subcommand_accepts_skills_flag(monkeypatch):
    import hermes_cli.main as main_mod

    captured = {}

    def fake_cmd_chat(args):
        captured["skills"] = args.skills
        captured["query"] = args.query

    monkeypatch.setattr(main_mod, "cmd_chat", fake_cmd_chat)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "chat", "-s", "github-auth", "-q", "hello"],
    )

    main_mod.main()

    assert captured == {
        "skills": ["github-auth"],
        "query": "hello",
    }


def test_chat_subcommand_accepts_image_flag(monkeypatch):
    import hermes_cli.main as main_mod

    captured = {}

    def fake_cmd_chat(args):
        captured["query"] = args.query
        captured["image"] = args.image

    monkeypatch.setattr(main_mod, "cmd_chat", fake_cmd_chat)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "chat", "-q", "hello", "--image", "~/storage/shared/Pictures/cat.png"],
    )

    main_mod.main()

    assert captured == {
        "query": "hello",
        "image": "~/storage/shared/Pictures/cat.png",
    }


def test_chat_subcommand_accepts_hidden_todo_snapshot_path(monkeypatch):
    import hermes_cli.main as main_mod

    captured = {}

    def fake_cmd_chat(args):
        captured["query"] = args.query
        captured["todo_snapshot_path"] = args.todo_snapshot_path

    monkeypatch.setattr(main_mod, "cmd_chat", fake_cmd_chat)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "chat", "-q", "hello", "--todo-snapshot-path", "/tmp/todo-snapshot.json"],
    )

    main_mod.main()

    assert captured == {
        "query": "hello",
        "todo_snapshot_path": "/tmp/todo-snapshot.json",
    }


def test_cmd_chat_forwards_todo_snapshot_path_to_cli(monkeypatch):
    import hermes_cli.main as main_mod

    captured = {}
    fake_cli = types.ModuleType("cli")
    setattr(fake_cli, "main", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    monkeypatch.setattr(main_mod, "_resolve_use_tui", lambda _args: False)
    monkeypatch.setattr(main_mod, "_apply_safe_mode", lambda _args: None)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: None)
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)

    args = types.SimpleNamespace(
        model=None,
        provider=None,
        toolsets=None,
        skills=None,
        verbose=False,
        quiet=True,
        query="hello",
        image=None,
        resume=None,
        worktree=False,
        checkpoints=False,
        pass_session_id=False,
        max_turns=None,
        ignore_rules=False,
        safe_mode=False,
        ignore_user_config=False,
        compact=False,
        todo_snapshot_path="/tmp/todo-snapshot.json",
    )

    main_mod.cmd_chat(args)

    assert captured["todo_snapshot_path"] == "/tmp/todo-snapshot.json"

def test_continue_worktree_and_skills_flags_work_together(monkeypatch):
    import hermes_cli.main as main_mod

    captured = {}

    def fake_cmd_chat(args):
        captured["continue_last"] = args.continue_last
        captured["worktree"] = args.worktree
        captured["skills"] = args.skills
        captured["command"] = args.command

    monkeypatch.setattr(main_mod, "cmd_chat", fake_cmd_chat)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "-c", "-w", "-s", "hermes-agent-dev"],
    )

    main_mod.main()

    assert captured == {
        "continue_last": True,
        "worktree": True,
        "skills": ["hermes-agent-dev"],
        "command": "chat",
    }
