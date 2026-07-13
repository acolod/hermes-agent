#!/usr/bin/env python3
"""Alex-only helper for explicitly shared Sam household facts."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

DEFAULT_PATH = Path.home() / ".hermes" / "shared-context" / "sam-household.json"


def load(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "facts": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        raise ValueError(f"Invalid shared-context file: {path}")
    return data


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List, add, or remove facts explicitly shared with Sam."
    )
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("list")
    add = subparsers.add_parser("add")
    add.add_argument("fact_id")
    add.add_argument("text")
    remove = subparsers.add_parser("remove")
    remove.add_argument("fact_id")
    args = parser.parse_args()

    data = load(args.path.expanduser())
    facts = data["facts"]
    if args.action == "list":
        print(json.dumps(facts, indent=2, ensure_ascii=False))
        return 0

    fact_id = args.fact_id.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", fact_id):
        parser.error("fact_id must be 2-64 lowercase letters, numbers, or hyphens")
    existing = next((item for item in facts if item.get("id") == fact_id), None)

    if args.action == "add":
        text = args.text.strip()
        if not text:
            parser.error("text must not be empty")
        if existing:
            existing["text"] = text
        else:
            facts.append({"id": fact_id, "text": text})
    elif existing:
        facts.remove(existing)
    else:
        parser.error(f"unknown fact_id: {fact_id}")

    save(args.path.expanduser(), data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
