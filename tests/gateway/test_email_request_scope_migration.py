import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "migrate_email_request_scope.py"
spec = importlib.util.spec_from_file_location("email_scope_migration", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def test_migration_is_targeted_idempotent_and_preserves_history(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    target = module.TARGET
    other = "agent:main:telegram:dm:42"
    (sessions_dir / "sessions.json").write_text(json.dumps({
        target: {"session_id": "email-history"},
        other: {"session_id": "telegram-history"},
    }))
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.executescript("""
            CREATE TABLE gateway_routing (
                scope TEXT NOT NULL,
                session_key TEXT NOT NULL,
                entry_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (scope, session_key)
            );
            CREATE TABLE sessions (session_id TEXT PRIMARY KEY, messages_json TEXT);
        """)
        conn.execute(
            "INSERT INTO gateway_routing VALUES (?, ?, ?, ?)",
            ("scope", target, "{}", 1.0),
        )
        conn.execute(
            "INSERT INTO gateway_routing VALUES (?, ?, ?, ?)",
            ("scope", other, "{}", 1.0),
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?)",
            ("email-history", "[\"preserved\"]"),
        )

    first = module.migrate(tmp_path, apply=True)
    second = module.migrate(tmp_path, apply=True)

    assert first == {"target": target, "sessions_removed": True, "routing_removed": 1, "applied_at": first["applied_at"]}
    assert second == {"target": target, "sessions_removed": False, "routing_removed": 0}
    sessions = json.loads((sessions_dir / "sessions.json").read_text())
    assert sessions == {other: {"session_id": "telegram-history"}}
    with sqlite3.connect(tmp_path / "state.db") as conn:
        assert conn.execute("SELECT session_key FROM gateway_routing").fetchall() == [(other,)]
        assert conn.execute("SELECT messages_json FROM sessions").fetchone()[0] == '["preserved"]'
