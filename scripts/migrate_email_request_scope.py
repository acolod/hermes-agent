#!/usr/bin/env python3
"""Idempotently retire reusable email DM routing without deleting transcripts."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from hermes_constants import get_hermes_home


TARGET = "agent:main:email:dm:alexcolodner@gmail.com"


def migrate(home: Path, *, apply: bool) -> dict:
    sessions_path = home / "sessions" / "sessions.json"
    db_path = home / "state.db"
    result = {"target": TARGET, "sessions_removed": False, "routing_removed": 0}

    sessions = json.loads(sessions_path.read_text()) if sessions_path.exists() else {}
    if isinstance(sessions, dict) and TARGET in sessions:
        result["sessions_removed"] = True
        if apply:
            sessions.pop(TARGET, None)
            tmp = sessions_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(sessions, indent=2, sort_keys=True) + "\n")
            tmp.replace(sessions_path)

    if db_path.exists():
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM gateway_routing WHERE session_key = ?",
                (TARGET,),
            ).fetchone()
            result["routing_removed"] = int(row[0] if row else 0)
            if apply:
                conn.execute(
                    "DELETE FROM gateway_routing WHERE session_key = ?",
                    (TARGET,),
                )
                conn.commit()

    if apply and (result["sessions_removed"] or result["routing_removed"]):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        result["applied_at"] = stamp
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--home", type=Path, default=get_hermes_home())
    args = parser.parse_args()

    home = args.home.expanduser().resolve()
    if args.apply:
        backup = home / "backups" / (
            "email-routing-migration-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        backup.mkdir(parents=True, exist_ok=False)
        for source in (home / "sessions" / "sessions.json", home / "state.db"):
            if source.exists():
                shutil.copy2(source, backup / source.name)
        print(json.dumps({"backup": str(backup)}))
    print(json.dumps(migrate(home, apply=args.apply), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
