from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_disk_cleanup():
    path = Path(__file__).parents[1] / "plugins" / "disk-cleanup" / "disk_cleanup.py"
    spec = importlib.util.spec_from_file_location("disk_cleanup_test_module", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guess_category_never_tracks_test_source_inside_git_worktree(tmp_path, monkeypatch) -> None:
    cleanup = _load_disk_cleanup()
    repo = tmp_path / "pregnancy"
    (repo / ".git").mkdir(parents=True)
    test_path = repo / "tests" / "test_editorial_critic.py"
    test_path.parent.mkdir()
    test_path.write_text("def test_example(): pass\n")
    monkeypatch.setattr(cleanup, "get_hermes_home", lambda: tmp_path)

    assert cleanup.guess_category(test_path) is None


def test_guess_category_still_tracks_non_repository_ephemeral_test(tmp_path, monkeypatch) -> None:
    cleanup = _load_disk_cleanup()
    test_path = tmp_path / "scratch" / "test_ephemeral.py"
    test_path.parent.mkdir()
    test_path.write_text("pass\n")
    monkeypatch.setattr(cleanup, "get_hermes_home", lambda: tmp_path)

    assert cleanup.guess_category(test_path) == "test"
