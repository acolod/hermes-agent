from gateway.background_task_ledger import BackgroundTaskLedger


def test_ledger_registers_updates_and_explicitly_clears(tmp_path):
    ledger = BackgroundTaskLedger(tmp_path)
    ledger.register("bg_one", {"chat_id": "chat", "task_items": []})
    ledger.update_todos("bg_one", [{"id": "one", "content": "Inspect", "status": "in_progress"}])
    assert ledger.active_records()["bg_one"]["task_items"][0]["content"] == "Inspect"
    ledger.clear("bg_one")
    assert ledger.active_records() == {}


def test_ledger_recovers_malformed_payload_and_isolates_concurrent_tasks(tmp_path):
    ledger = BackgroundTaskLedger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text("not-json", encoding="utf-8")
    ledger.register("bg_one", {"chat_id": "one"})
    ledger.register("bg_two", {"chat_id": "two"})
    assert set(ledger.active_records()) == {"bg_one", "bg_two"}
    ledger.clear("bg_one")
    assert set(ledger.active_records()) == {"bg_two"}


def test_ledger_mutation_failures_are_nonfatal(monkeypatch, tmp_path):
    ledger = BackgroundTaskLedger(tmp_path)
    monkeypatch.setattr(ledger, "_save", lambda _records: (_ for _ in ()).throw(OSError("read-only")))

    assert ledger.register("bg_one", {"chat_id": "one"}) is False
    assert ledger.update_todos("bg_one", []) is False
    assert ledger.update_metadata("bg_one", state="running") is False
    assert ledger.clear("bg_one") is False
