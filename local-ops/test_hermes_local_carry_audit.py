from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("hermes-local-carry-audit.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("hermes_local_carry_audit", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _commit(repo: Path, subject: str, filename: str, content: str) -> str:
    (repo / filename).parent.mkdir(parents=True, exist_ok=True)
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def _ledger(
    functional: list[object],
    administrative: list[str] | None = None,
    upstream_merges: list[str] | None = None,
    base_ref: str = "origin/main",
    live_ref: str = "local/live",
) -> str:
    manifest = {
        "version": 1,
        "base_ref": base_ref,
        "live_ref": live_ref,
        "functional": functional,
        "administrative": administrative or [],
        "upstream_merges": upstream_merges or [],
    }
    return (
        "# local/live carry ledger\n\n"
        "## Audit manifest\n\n"
        "```json carry-audit\n"
        + json.dumps(manifest, indent=2)
        + "\n```\n\n"
        "## Current functional carries\n\n"
        "Fixture carry documentation.\n"
    )


@pytest.fixture
def healthy_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Audit Test")
    _git(repo, "config", "user.email", "audit@example.invalid")
    base = _commit(repo, "base", "base.txt", "base\n")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    _git(repo, "switch", "-c", "local/live")
    carry = _commit(repo, "functional carry", "feature.txt", "feature\n")
    _commit(repo, "docs: record carry ledger", "LOCAL_LIVE_CARRIES.md", _ledger([carry]))
    return repo, carry


def test_healthy_repo_is_silent_and_read_only(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, _ = healthy_repo
    before_head = _git(repo, "rev-parse", "HEAD")
    before_reflog = _git(repo, "reflog", "--format=%H")
    before_ledger = (repo / "LOCAL_LIVE_CARRIES.md").read_bytes()

    result = module.audit_repo(repo)

    assert result.ok is True
    assert result.render() == "[SILENT]"
    assert _git(repo, "rev-parse", "HEAD") == before_head
    assert _git(repo, "reflog", "--format=%H") == before_reflog
    assert (repo / "LOCAL_LIVE_CARRIES.md").read_bytes() == before_ledger


def test_missing_ledger_fails_closed(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, _ = healthy_repo
    (repo / "LOCAL_LIVE_CARRIES.md").unlink()

    result = module.audit_repo(repo)

    assert result.ok is False
    assert "ledger" in result.render().lower()


def test_dirty_tracked_and_untracked_files_are_reported(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, _ = healthy_repo
    (repo / "feature.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "unexpected.tmp").write_text("untracked\n", encoding="utf-8")

    result = module.audit_repo(repo)

    assert result.ok is False
    text = result.render()
    assert "feature.txt" in text
    assert "unexpected.tmp" in text


def test_unreachable_declared_carry_is_rejected(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, _ = healthy_repo
    other = _git(repo, "commit-tree", _git(repo, "write-tree"), "-m", "orphan")
    ledger = _ledger([other])
    (repo / "LOCAL_LIVE_CARRIES.md").write_text(ledger, encoding="utf-8")
    _git(repo, "add", "LOCAL_LIVE_CARRIES.md")
    _git(repo, "commit", "-m", "docs: replace ledger")

    result = module.audit_repo(repo)

    assert result.ok is False
    assert "not reachable" in result.render().lower()


def test_uncovered_functional_commit_is_rejected(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, carry = healthy_repo
    second = _commit(repo, "unrecorded functional carry", "second.txt", "second\n")

    result = module.audit_repo(repo)

    assert result.ok is False
    text = result.render()
    assert second[:10] in text
    assert "uncovered" in text.lower()


def test_locally_known_base_must_be_contained(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, _ = healthy_repo
    _git(repo, "switch", "main")
    newer = _commit(repo, "new upstream", "upstream.txt", "new\n")
    _git(repo, "update-ref", "refs/remotes/origin/main", newer)
    _git(repo, "switch", "local/live")

    result = module.audit_repo(repo)

    assert result.ok is False
    assert "does not contain locally known" in result.render().lower()


def test_command_runner_refuses_mutating_git_verbs(tmp_path: Path):
    module = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()

    for verb in ("fetch", "pull", "checkout", "switch", "reset", "merge", "rebase", "config"):
        with pytest.raises(ValueError, match="not allowed"):
            module.run_git(repo, verb)


@pytest.mark.parametrize(
    "args",
    [
        ("show", "--output=/tmp/audit-write", "HEAD"),
        ("show", "-o", "/tmp/audit-write", "HEAD"),
        ("diff-tree", "--ext-diff", "HEAD"),
        ("show", "--textconv", "HEAD"),
    ],
)
def test_command_runner_refuses_write_or_exec_options(tmp_path: Path, args: tuple[str, ...]):
    module = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(ValueError, match="not allowed"):
        module.run_git(repo, *args)


def test_command_runner_disables_optional_git_locks(healthy_repo: tuple[Path, str], monkeypatch):
    module = _load_module()
    repo, _ = healthy_repo
    real_run = module.subprocess.run
    observed: list[str | None] = []

    def spy(*args, **kwargs):
        observed.append((kwargs.get("env") or {}).get("GIT_OPTIONAL_LOCKS"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", spy)
    module.run_git(repo, "status", "--porcelain")

    assert observed == ["0"]


def test_ledger_path_must_remain_inside_repo(healthy_repo: tuple[Path, str], tmp_path: Path):
    module = _load_module()
    repo, _ = healthy_repo
    (tmp_path / "outside.md").write_text(_ledger([]), encoding="utf-8")

    result = module.audit_repo(repo, "../outside.md")

    assert result.ok is False
    assert "inside repository" in result.render().lower()


def test_ledger_symlink_must_not_escape_repo(healthy_repo: tuple[Path, str], tmp_path: Path):
    module = _load_module()
    repo, _ = healthy_repo
    outside = tmp_path / "outside-symlink-target.md"
    outside.write_text(_ledger([]), encoding="utf-8")
    (repo / "ledger-link.md").symlink_to(outside)

    result = module.audit_repo(repo, "ledger-link.md")

    assert result.ok is False
    assert "inside repository" in result.render().lower()


def test_administrative_commit_cannot_hide_code_change(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, carry = healthy_repo
    code_commit = _commit(repo, "code mislabeled as administrative", "source.py", "unsafe = True\n")
    _commit(
        repo,
        "docs: misclassify code",
        "LOCAL_LIVE_CARRIES.md",
        _ledger([carry], administrative=[code_commit]),
    )

    result = module.audit_repo(repo)

    assert result.ok is False
    assert "administrative commit" in result.render().lower()
    assert "non-documentation" in result.render().lower()


def test_manifest_refs_cannot_redirect_audit(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, carry = healthy_repo
    _commit(
        repo,
        "docs: redirect audit",
        "LOCAL_LIVE_CARRIES.md",
        _ledger([carry], base_ref="local/live", live_ref="local/live"),
    )

    result = module.audit_repo(repo)

    assert result.ok is False
    assert "fixed refs" in result.render().lower()


def test_manifest_rejects_unknown_top_level_keys(tmp_path: Path):
    module = _load_module()
    ledger = tmp_path / "LOCAL_LIVE_CARRIES.md"
    text = _ledger([]).replace(
        '  "version": 1,',
        '  "version": 1,\n  "unexpected": true,',
        1,
    )
    ledger.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="exact top-level keys"):
        module.load_manifest(ledger)


def test_manifest_rejects_multiple_fenced_blocks(tmp_path: Path):
    module = _load_module()
    ledger = tmp_path / "LOCAL_LIVE_CARRIES.md"
    second = _ledger([]).split("## Audit manifest", 1)[1]
    ledger.write_text(_ledger([]) + "\n## Conflicting manifest\n" + second, encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one fenced"):
        module.load_manifest(ledger)


def test_manifest_rejects_duplicate_json_keys(tmp_path: Path):
    module = _load_module()
    ledger = tmp_path / "LOCAL_LIVE_CARRIES.md"
    text = _ledger([]).replace(
        '  "base_ref": "origin/main",',
        '  "base_ref": "origin/main",\n  "base_ref": "local/live",',
        1,
    )
    ledger.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate key"):
        module.load_manifest(ledger)


def test_undeclared_first_parent_merge_is_rejected(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, _ = healthy_repo
    _git(repo, "switch", "-c", "local/topic")
    _commit(repo, "topic functionality", "topic.py", "enabled = True\n")
    _git(repo, "switch", "local/live")
    _git(repo, "merge", "--no-ff", "local/topic", "-m", "merge local topic")

    result = module.audit_repo(repo)

    assert result.ok is False
    assert "undeclared first-parent merge" in result.render().lower()


def test_declared_upstream_merge_is_accepted(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, carry = healthy_repo
    _git(repo, "switch", "main")
    upstream = _commit(repo, "new upstream", "upstream.txt", "new\n")
    _git(repo, "update-ref", "refs/remotes/origin/main", upstream)
    _git(repo, "switch", "local/live")
    _git(repo, "merge", "--no-ff", "main", "-m", "merge upstream main")
    merge_commit = _git(repo, "rev-parse", "HEAD")
    _commit(
        repo,
        "docs: record upstream merge",
        "LOCAL_LIVE_CARRIES.md",
        _ledger([carry], upstream_merges=[merge_commit]),
    )

    result = module.audit_repo(repo)

    assert result.ok is True
    assert result.render() == "[SILENT]"


def test_declared_local_side_merge_is_rejected(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, carry = healthy_repo
    _git(repo, "switch", "-c", "local/topic")
    _commit(repo, "local topic", "topic.py", "enabled = True\n")
    _git(repo, "switch", "local/live")
    _git(repo, "merge", "--no-ff", "local/topic", "-m", "merge local topic")
    merge_commit = _git(repo, "rev-parse", "HEAD")
    _commit(
        repo,
        "docs: misclassify local merge",
        "LOCAL_LIVE_CARRIES.md",
        _ledger([carry], upstream_merges=[merge_commit]),
    )

    result = module.audit_repo(repo)

    assert result.ok is False
    assert "not from locally known upstream" in result.render().lower()


def test_functional_entry_cannot_name_ledger_only_commit(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, carry = healthy_repo
    ledger_commit = _git(repo, "rev-parse", "HEAD")
    _commit(
        repo,
        "docs: self justify ledger",
        "LOCAL_LIVE_CARRIES.md",
        _ledger([carry, ledger_commit]),
    )

    result = module.audit_repo(repo)

    assert result.ok is False
    assert "ledger-only commit" in result.render().lower()


def test_cli_silent_output_has_no_trailing_newline(healthy_repo: tuple[Path, str]):
    repo, _ = healthy_repo

    completed = subprocess.run(
        ["python3", str(SCRIPT), "--repo", str(repo)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == "[SILENT]"
    assert completed.stderr == ""


def test_main_formats_unexpected_failure_without_traceback(monkeypatch, capsys, tmp_path: Path):
    module = _load_module()

    def fail(*_args, **_kwargs):
        raise OSError("simulated execution failure")

    monkeypatch.setattr(module, "audit_repo", fail)

    code = module.main(["--repo", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "attention required" in captured.out.lower()
    assert "simulated execution failure" in captured.out
    assert "traceback" not in captured.out.lower()
    assert captured.err == ""


def test_manifest_range_cannot_cross_upstream_merge(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, first = healthy_repo
    range_start = _commit(repo, "range start", "start.txt", "start\n")
    _git(repo, "switch", "main")
    upstream = _commit(repo, "new upstream", "upstream.txt", "new\n")
    _git(repo, "update-ref", "refs/remotes/origin/main", upstream)
    _git(repo, "switch", "local/live")
    _git(repo, "merge", "--no-ff", "main", "-m", "merge upstream main")
    merge_commit = _git(repo, "rev-parse", "HEAD")
    range_end = _commit(repo, "range end", "end.txt", "end\n")
    _commit(
        repo,
        "docs: record invalid spanning range",
        "LOCAL_LIVE_CARRIES.md",
        _ledger(
            [first, {"from": range_start, "through": range_end}],
            upstream_merges=[merge_commit],
        ),
    )

    result = module.audit_repo(repo)

    assert result.ok is False
    assert "crosses a merge" in result.render().lower()


def test_manifest_range_expands_first_parent_commits(healthy_repo: tuple[Path, str]):
    module = _load_module()
    repo, first = healthy_repo
    second = _commit(repo, "second functional carry", "second.txt", "second\n")
    third = _commit(repo, "third functional carry", "third.txt", "third\n")
    manifest = _ledger([first, {"from": second, "through": third}])
    _commit(repo, "docs: record range", "LOCAL_LIVE_CARRIES.md", manifest)

    result = module.audit_repo(repo)

    assert result.ok is True
    assert result.render() == "[SILENT]"
    assert second in module.expand_manifest_commits(repo, module.load_manifest(repo / "LOCAL_LIVE_CARRIES.md"))
