#!/usr/bin/env python3
"""Strictly read-only integrity audit for a local/live Hermes carry lane."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


_ALLOWED_GIT_VERBS = {
    "diff-tree",
    "merge-base",
    "rev-list",
    "rev-parse",
    "show",
    "status",
    "symbolic-ref",
}
_FORBIDDEN_GIT_OPTIONS = {"-o", "--ext-diff", "--textconv"}
_FORBIDDEN_GIT_OPTION_PREFIXES = ("--output=",)
_ADMINISTRATIVE_PATH_PREFIXES = ("docs/", ".hermes/plans/")
_LEDGER_ONLY_PATHS = {
    "LOCAL_LIVE_CARRIES.md",
    "local-ops/carry-ledger-evidence.md",
}
_MANIFEST_RE = re.compile(
    r"```json\s+carry-audit\s*\n(?P<body>.*?)\n```", re.DOTALL
)
_FULL_HASH_RE = re.compile(r"^[0-9a-f]{40}$")
_EXPECTED_BASE_REF = "origin/main"
_EXPECTED_LIVE_REF = "local/live"
_MANIFEST_KEYS = {
    "version",
    "base_ref",
    "live_ref",
    "functional",
    "administrative",
    "upstream_merges",
}


class AuditResult:
    def __init__(self, issues: list[str] | None = None) -> None:
        self.issues = issues or []

    @property
    def ok(self) -> bool:
        return not self.issues

    def render(self) -> str:
        if self.ok:
            return "[SILENT]"
        lines = ["Hermes local/live carry audit: attention required"]
        lines.extend(f"- {issue}" for issue in self.issues)
        return "\n".join(lines)


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one allow-listed read-only Git command."""
    if not args or args[0] not in _ALLOWED_GIT_VERBS:
        verb = args[0] if args else "<missing>"
        raise ValueError(f"git verb {verb!r} is not allowed in the read-only audit")
    if any(
        arg in _FORBIDDEN_GIT_OPTIONS
        or any(arg.startswith(prefix) for prefix in _FORBIDDEN_GIT_OPTION_PREFIXES)
        for arg in args[1:]
    ):
        raise ValueError("write-capable or external-exec git option is not allowed")
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result


def _git_stdout(repo: Path, *args: str) -> str:
    return run_git(repo, *args).stdout.strip()


def load_manifest(ledger_path: Path) -> dict[str, Any]:
    text = ledger_path.read_text(encoding="utf-8")
    matches = list(_MANIFEST_RE.finditer(text))
    if len(matches) != 1:
        raise ValueError("ledger must have exactly one fenced `json carry-audit` manifest")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError(f"ledger audit manifest has duplicate key {key!r}")
            parsed[key] = value
        return parsed

    try:
        manifest = json.loads(
            matches[0].group("body"), object_pairs_hook=reject_duplicate_keys
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"ledger audit manifest is invalid JSON: {exc.msg}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("ledger audit manifest must be an object with version 1")
    if set(manifest) != _MANIFEST_KEYS:
        raise ValueError(
            "ledger audit manifest must use exact top-level keys: "
            + ", ".join(sorted(_MANIFEST_KEYS))
        )
    if manifest.get("version") != 1:
        raise ValueError("ledger audit manifest must be an object with version 1")
    for key in ("base_ref", "live_ref"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f"ledger audit manifest requires non-empty {key}")
    if (
        manifest["base_ref"] != _EXPECTED_BASE_REF
        or manifest["live_ref"] != _EXPECTED_LIVE_REF
    ):
        raise ValueError(
            "ledger audit manifest must use fixed refs "
            f"{_EXPECTED_BASE_REF} and {_EXPECTED_LIVE_REF}"
        )
    for key in ("functional", "administrative", "upstream_merges"):
        if not isinstance(manifest.get(key), list):
            raise ValueError(f"ledger audit manifest requires a {key} list")
    return manifest


def _resolve_commit(repo: Path, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("manifest commit references must be non-empty strings")
    resolved = _git_stdout(repo, "rev-parse", f"{value.strip()}^{{commit}}")
    if not _FULL_HASH_RE.fullmatch(resolved):
        raise ValueError(f"could not resolve commit reference {value!r}")
    return resolved


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode
        == 0
    )


def _commit_parents(repo: Path, commit: str) -> list[str]:
    fields = _git_stdout(repo, "rev-list", "--parents", "-n", "1", commit).split()
    if not fields or fields[0] != commit:
        raise ValueError(f"could not inspect parents for commit {commit}")
    return fields[1:]


def _lane_commits(
    repo: Path, base_ref: str, live_ref: str
) -> tuple[list[str], set[str], set[str]]:
    merge_base = _git_stdout(repo, "merge-base", base_ref, live_ref)
    lane = _git_stdout(
        repo,
        "rev-list",
        "--first-parent",
        "--reverse",
        f"{merge_base}..{live_ref}",
    ).splitlines()
    merges: set[str] = set()
    non_merges: set[str] = set()
    for commit in lane:
        (merges if len(_commit_parents(repo, commit)) > 1 else non_merges).add(commit)
    return lane, non_merges, merges


def _changed_files(repo: Path, commit: str) -> set[str]:
    return set(
        _git_stdout(
            repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit
        ).splitlines()
    )


def _is_ledger_only_commit(repo: Path, commit: str) -> bool:
    files = _changed_files(repo, commit)
    return bool(files) and files <= _LEDGER_ONLY_PATHS


def expand_manifest_commits(repo: Path, manifest: dict[str, Any]) -> set[str]:
    """Resolve functional single commits and merge-free first-parent ranges."""
    live_ref = manifest["live_ref"]
    live_commit = _resolve_commit(repo, live_ref)
    _, lane_non_merges, _ = _lane_commits(repo, manifest["base_ref"], live_ref)
    expanded: set[str] = set()
    for entry in manifest["functional"]:
        if isinstance(entry, str):
            commit = _resolve_commit(repo, entry)
            if not _is_ancestor(repo, commit, live_commit):
                raise ValueError(f"declared carry {entry} is not reachable from {live_ref}")
            if _is_ledger_only_commit(repo, commit):
                raise ValueError(f"declared carry {entry} is a ledger-only commit")
            if commit not in lane_non_merges:
                raise ValueError(
                    f"declared carry {entry} is not a non-merge first-parent {live_ref} commit"
                )
            expanded.add(commit)
            continue
        if not isinstance(entry, dict) or set(entry) != {"from", "through"}:
            raise ValueError("functional entries must be commit strings or {from, through} ranges")
        start = _resolve_commit(repo, entry["from"])
        end = _resolve_commit(repo, entry["through"])
        if not _is_ancestor(repo, start, end):
            raise ValueError(
                f"declared carry range {entry['from']}..{entry['through']} is not ordered"
            )
        if not _is_ancestor(repo, end, live_commit):
            raise ValueError(
                f"declared carry range ending {entry['through']} is not reachable from {live_ref}"
            )
        if start not in lane_non_merges or end not in lane_non_merges:
            raise ValueError(
                f"declared carry range {entry['from']}..{entry['through']} must use "
                f"non-merge first-parent {live_ref} endpoints"
            )
        commits = _git_stdout(
            repo,
            "rev-list",
            "--first-parent",
            "--reverse",
            f"{start}^..{end}",
        ).splitlines()
        if start not in commits or end not in commits:
            raise ValueError(
                f"declared carry range {entry['from']}..{entry['through']} is not a first-parent range"
            )
        if any(len(_commit_parents(repo, commit)) > 1 for commit in commits):
            raise ValueError(
                f"declared carry range {entry['from']}..{entry['through']} crosses a merge"
            )
        if any(_is_ledger_only_commit(repo, commit) for commit in commits):
            raise ValueError(
                f"declared carry range {entry['from']}..{entry['through']} includes a ledger-only commit"
            )
        expanded.update(commits)
    return expanded


def _administrative_commits(repo: Path, manifest: dict[str, Any]) -> set[str]:
    live_ref = manifest["live_ref"]
    live_commit = _resolve_commit(repo, live_ref)
    _, lane_non_merges, _ = _lane_commits(repo, manifest["base_ref"], live_ref)
    commits: set[str] = set()
    for entry in manifest["administrative"]:
        commit = _resolve_commit(repo, entry)
        if not _is_ancestor(repo, commit, live_commit):
            raise ValueError(f"administrative commit {entry} is not reachable from {live_ref}")
        if commit not in lane_non_merges:
            raise ValueError(
                f"administrative commit {entry} is not a non-merge first-parent {live_ref} commit"
            )
        files = _changed_files(repo, commit)
        non_documentation = sorted(
            path
            for path in files
            if path not in _LEDGER_ONLY_PATHS
            and not path.startswith(_ADMINISTRATIVE_PATH_PREFIXES)
        )
        if not files or non_documentation:
            detail = ", ".join(non_documentation) if non_documentation else "no changed paths"
            raise ValueError(
                f"administrative commit {entry} changes non-documentation paths: {detail}"
            )
        commits.add(commit)
    return commits


def _validate_upstream_merges(
    repo: Path,
    manifest: dict[str, Any],
    base_commit: str,
    lane_merges: set[str],
) -> None:
    declared: set[str] = set()
    for entry in manifest["upstream_merges"]:
        commit = _resolve_commit(repo, entry)
        if commit not in lane_merges:
            raise ValueError(f"declared upstream merge {entry} is not a first-parent lane merge")
        parents = _commit_parents(repo, commit)
        if len(parents) != 2:
            raise ValueError(f"declared upstream merge {entry} must have exactly two parents")
        if not _is_ancestor(repo, parents[1], base_commit):
            raise ValueError(
                f"declared upstream merge {entry} is not from locally known upstream"
            )
        declared.add(commit)
    missing = sorted(lane_merges - declared)
    if missing:
        raise ValueError(
            "undeclared first-parent merge commits: "
            + ", ".join(commit[:10] for commit in missing)
        )


def audit_repo(repo: Path, ledger_name: str = "LOCAL_LIVE_CARRIES.md") -> AuditResult:
    repo = Path(repo).resolve()
    ledger_path = (repo / ledger_name).resolve()
    issues: list[str] = []

    try:
        ledger_path.relative_to(repo)
    except ValueError:
        return AuditResult(["carry ledger path must remain inside repository root"])

    if not ledger_path.is_file():
        return AuditResult([f"carry ledger is missing: {ledger_path}"])

    try:
        manifest = load_manifest(ledger_path)
    except (OSError, ValueError) as exc:
        return AuditResult([str(exc)])

    base_ref = manifest["base_ref"]
    live_ref = manifest["live_ref"]
    try:
        base_commit = _resolve_commit(repo, base_ref)
        live_commit = _resolve_commit(repo, live_ref)
    except (RuntimeError, ValueError) as exc:
        return AuditResult([str(exc)])

    current = run_git(repo, "symbolic-ref", "--short", "HEAD", check=False)
    current_branch = current.stdout.strip() if current.returncode == 0 else "<detached>"
    if current_branch != live_ref:
        issues.append(f"runtime branch is {current_branch}; expected {live_ref}")

    if not _is_ancestor(repo, base_commit, live_commit):
        issues.append(
            f"{live_ref} does not contain locally known {base_ref} at {base_commit[:10]}"
        )

    status_lines = _git_stdout(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    if status_lines:
        issues.append("worktree is dirty: " + ", ".join(line.strip() for line in status_lines))

    try:
        lane_commits, lane_non_merges, lane_merges = _lane_commits(repo, base_ref, live_ref)
        _validate_upstream_merges(repo, manifest, base_commit, lane_merges)
        declared = expand_manifest_commits(repo, manifest)
        administrative = _administrative_commits(repo, manifest)
        uncovered = [
            commit
            for commit in lane_commits
            if commit in lane_non_merges
            and commit not in declared
            and commit not in administrative
            and not _is_ledger_only_commit(repo, commit)
        ]
        if uncovered:
            summaries = []
            for commit in uncovered[:8]:
                subject = _git_stdout(repo, "show", "-s", "--format=%s", commit)
                summaries.append(f"{commit[:10]} {subject}")
            suffix = f" (+{len(uncovered) - 8} more)" if len(uncovered) > 8 else ""
            issues.append("uncovered local/live commits: " + "; ".join(summaries) + suffix)
    except (RuntimeError, ValueError) as exc:
        issues.append(str(exc))

    return AuditResult(issues)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--ledger", default="LOCAL_LIVE_CARRIES.md")
    args = parser.parse_args(argv)
    try:
        result = audit_repo(args.repo, args.ledger)
    except Exception as exc:
        result = AuditResult([f"audit execution failed: {exc}"])
    sys.stdout.write(result.render())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
