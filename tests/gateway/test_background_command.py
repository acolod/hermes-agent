"""Tests for /background gateway slash command.

Tests the _handle_background_command handler (run a prompt in a separate
background session) across gateway messenger platforms.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/background", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner with minimal mocks."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._background_tasks = set()
    runner._background_task_ledger_for_source = GatewayRunner._background_task_ledger_for_source.__get__(runner)
    runner._register_background_task_ledger = GatewayRunner._register_background_task_ledger.__get__(runner)

    mock_store = MagicMock()
    runner.session_store = mock_store

    from gateway.hooks import HookRegistry
    runner.hooks = HookRegistry()

    return runner


# ---------------------------------------------------------------------------
# _handle_background_command
# ---------------------------------------------------------------------------


class TestHandleBackgroundCommand:
    """Tests for GatewayRunner._handle_background_command."""

    @pytest.mark.asyncio
    async def test_no_prompt_shows_usage(self):
        """Running /background with no prompt shows usage."""
        runner = _make_runner()
        event = _make_event(text="/background")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result
        assert "/background" in result

    @pytest.mark.asyncio
    async def test_bg_alias_no_prompt_shows_usage(self):
        """Running /bg with no prompt shows usage."""
        runner = _make_runner()
        event = _make_event(text="/bg")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result

    @pytest.mark.asyncio
    async def test_empty_prompt_shows_usage(self):
        """Running /background with only whitespace shows usage."""
        runner = _make_runner()
        event = _make_event(text="/background   ")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result

    @pytest.mark.asyncio
    async def test_valid_prompt_starts_task(self):
        """Running /background with a prompt returns confirmation and starts task."""
        runner = _make_runner()
        runner._emit_gateway_activity = MagicMock(return_value=None)
        runner._register_background_task_ledger = MagicMock()


        # Patch asyncio.create_task to capture the coroutine
        created_tasks = []
        original_create_task = asyncio.create_task

        def capture_task(coro, *args, **kwargs):
            # Close the coroutine to avoid warnings
            coro.close()
            mock_task = MagicMock()
            created_tasks.append(mock_task)
            return mock_task

        with patch("gateway.run.asyncio.create_task", side_effect=capture_task):
            event = _make_event(text="/background Summarize the top HN stories")
            result = await runner._handle_background_command(event)

        assert "🔄" in result
        assert "Background task started" in result
        assert "bg_" in result  # task ID starts with bg_
        assert "Summarize the top HN stories" in result
        assert len(created_tasks) == 1  # background task was created
        runner._register_background_task_ledger.assert_called_once()
        lifecycle = runner._emit_gateway_activity.call_args.kwargs
        assert lifecycle["kind"] == "background"
        assert lifecycle["phase"] == "background-start"
        assert lifecycle["terminal"] is False
        assert lifecycle["task_items"] == [
            {"id": "background", "content": "Run background task", "status": "in_progress"}
        ]

    @pytest.mark.asyncio
    async def test_telegram_dm_topic_passes_trigger_anchor_to_task(self):
        """Telegram private-topic completion sends need the original command message id."""
        runner = _make_runner()
        runner._run_background_task = AsyncMock()

        def capture_task(coro, *args, **kwargs):
            coro.close()
            mock_task = MagicMock()
            return mock_task

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            chat_type="dm",
            thread_id="20197",
        )
        event = MessageEvent(
            text="/background summarize",
            source=source,
            message_id="463",
            reply_to_message_id="462",
        )

        with patch("gateway.run.asyncio.create_task", side_effect=capture_task):
            result = await runner._handle_background_command(event)

        assert "Background task started" in result
        runner._run_background_task.assert_called_once()
        assert runner._run_background_task.call_args.kwargs["event_message_id"] == "463"

    @pytest.mark.asyncio
    async def test_prompt_truncated_in_preview(self):
        """Long prompts are truncated to 60 chars in the confirmation message."""
        runner = _make_runner()
        long_prompt = "A" * 100

        with patch("gateway.run.asyncio.create_task", side_effect=lambda c, **kw: (c.close(), MagicMock())[1]):
            event = _make_event(text=f"/background {long_prompt}")
            result = await runner._handle_background_command(event)

        assert "..." in result
        # Should not contain the full prompt
        assert long_prompt not in result

    @pytest.mark.asyncio
    async def test_task_id_is_unique(self):
        """Each background task gets a unique task ID."""
        runner = _make_runner()
        task_ids = set()

        with patch("gateway.run.asyncio.create_task", side_effect=lambda c, **kw: (c.close(), MagicMock())[1]):
            for i in range(5):
                event = _make_event(text=f"/background task {i}")
                result = await runner._handle_background_command(event)
                # Extract task ID from result (format: "Task ID: bg_HHMMSS_hex")
                for line in result.split("\n"):
                    if "Task ID:" in line:
                        tid = line.split("Task ID:")[1].strip()
                        task_ids.add(tid)

        assert len(task_ids) == 5  # all unique

    @pytest.mark.asyncio
    async def test_works_across_platforms(self):
        """The /background command works for all platforms."""
        for platform in [Platform.TELEGRAM, Platform.DISCORD, Platform.SLACK]:
            runner = _make_runner()
            with patch("gateway.run.asyncio.create_task", side_effect=lambda c, **kw: (c.close(), MagicMock())[1]):
                event = _make_event(
                    text="/background test task",
                    platform=platform,
                )
                result = await runner._handle_background_command(event)
                assert "Background task started" in result

# ---------------------------------------------------------------------------
# _run_background_task
# ---------------------------------------------------------------------------


class TestRunBackgroundTask:
    """Tests for GatewayRunner._run_background_task (the actual execution)."""

    @pytest.mark.asyncio
    async def test_no_adapter_returns_silently(self):
        """When no adapter is available, the task returns without error."""
        runner = _make_runner()
        runner._emit_gateway_activity = MagicMock(return_value=None)
        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )
        # No adapters set — should not raise
        await runner._run_background_task("test prompt", source, "bg_test")
        lifecycle = runner._emit_gateway_activity.call_args.kwargs
        assert lifecycle["phase"] == "failed"
        assert lifecycle["kind"] == "background"
        assert lifecycle["terminal"] is True
    @pytest.mark.asyncio
    async def test_no_credentials_sends_error(self):
        """When provider credentials are missing, an error is sent."""
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": None}):
            await runner._run_background_task("test prompt", source, "bg_test")

        # Should have sent an error message
        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        assert "failed" in call_args[1].get("content", call_args[0][1] if len(call_args[0]) > 1 else "").lower()

    @pytest.mark.asyncio
    async def test_no_credentials_send_failure_emits_one_terminal_event(self):
        """A failed error delivery must not duplicate the lifecycle terminal."""
        runner = _make_runner()
        runner._emit_gateway_activity = MagicMock(return_value=None)
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock(side_effect=RuntimeError("offline"))
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
        )

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value={"api_key": None},
        ):
            await runner._run_background_task("test prompt", source, "bg_test")

        assert runner._emit_gateway_activity.call_count == 1
        assert runner._emit_gateway_activity.call_args.kwargs["phase"] == "failed"

    @pytest.mark.asyncio
    async def test_successful_task_sends_result(self):
        """When the agent completes successfully, the result is sent."""
        runner = _make_runner()
        runner._emit_gateway_activity = MagicMock(return_value=None)
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        mock_adapter.extract_media = MagicMock(return_value=([], "Hello from background!"))
        mock_adapter.extract_images = MagicMock(return_value=([], "Hello from background!"))
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        mock_result = {"final_response": "Hello from background!", "messages": []}

        checkpoint_config = {
            "checkpoints": {
                "enabled": True,
                "max_snapshots": 8,
                "max_total_size_mb": 222,
                "max_file_size_mb": 3,
            }
        }
        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("gateway.run._load_gateway_config", return_value=checkpoint_config), \
             patch("run_agent.AIAgent") as MockAgent:
            mock_agent_instance = MagicMock()
            mock_agent_instance.shutdown_memory_provider = MagicMock()
            mock_agent_instance.close = MagicMock()
            mock_agent_instance.run_conversation.return_value = mock_result
            MockAgent.return_value = mock_agent_instance

            await runner._run_background_task("say hello", source, "bg_test")

        # Should have sent the result
        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        content = call_args[1].get("content", call_args[0][1] if len(call_args[0]) > 1 else "")
        assert "Background task complete" in content
        assert "Hello from background!" in content
        agent_kwargs = MockAgent.call_args.kwargs
        assert agent_kwargs["checkpoints_enabled"] is True
        assert agent_kwargs["checkpoint_max_snapshots"] == 8
        assert agent_kwargs["checkpoint_max_total_size_mb"] == 222
        assert agent_kwargs["checkpoint_max_file_size_mb"] == 3
        mock_agent_instance.shutdown_memory_provider.assert_called_once()
        mock_agent_instance.close.assert_called_once()
        lifecycle = runner._emit_gateway_activity.call_args.kwargs
        assert lifecycle["phase"] == "completed"
        assert lifecycle["kind"] == "background"
        assert lifecycle["terminal"] is True

    @pytest.mark.asyncio
    async def test_successful_task_settles_terminal_card_before_result_delivery(self):
        runner = _make_runner()
        acknowledgement = asyncio.get_running_loop().create_future()
        acknowledgement.set_result(True)
        runner._emit_gateway_activity = MagicMock(return_value=acknowledgement)
        delivery_order: list[str] = []

        async def _await_card(_ack):
            delivery_order.append("card")

        runner._await_terminal_card_publication = AsyncMock(side_effect=_await_card)
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock(side_effect=lambda *_args, **_kwargs: delivery_order.append("result"))
        mock_adapter.extract_media = MagicMock(return_value=([], "done"))
        mock_adapter.extract_images = MagicMock(return_value=([], "done"))
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = SessionSource(platform=Platform.TELEGRAM, user_id="12345", chat_id="67890")

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "done", "messages": []}
            MockAgent.return_value = mock_agent
            await runner._run_background_task("say hello", source, "bg_test")

        runner._await_terminal_card_publication.assert_awaited_once()
        assert delivery_order == ["card", "result"]

    @pytest.mark.asyncio
    async def test_background_todo_step_updates_working_card_and_terminal_snapshot(self):
        runner = _make_runner()
        runner._emit_gateway_activity = MagicMock(return_value=None)
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        mock_adapter.extract_media = MagicMock(return_value=([], "done"))
        mock_adapter.extract_images = MagicMock(return_value=([], "done"))
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = SessionSource(platform=Platform.TELEGRAM, user_id="12345", chat_id="67890")
        todos = [{"id": "inspect", "content": "Inspect current state", "status": "in_progress"}]

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent._todo_store.read.return_value = todos

            def _run_conversation(**_kwargs):
                MockAgent.call_args.kwargs["step_callback"](
                    1,
                    [{"name": "todo", "result": {"todos": todos}}],
                )
                return {"final_response": "done", "messages": []}

            mock_agent.run_conversation.side_effect = _run_conversation
            MockAgent.return_value = mock_agent
            await runner._run_background_task("say hello", source, "bg_test")

        activities = [call.kwargs for call in runner._emit_gateway_activity.call_args_list]
        working = next(activity for activity in activities if activity["phase"] == "working")
        terminal = next(activity for activity in activities if activity["terminal"])
        assert working["task_items"] == todos
        assert terminal["task_items"] == todos

    @pytest.mark.asyncio
    async def test_cancelled_background_task_carries_latest_todo_snapshot(self):
        runner = _make_runner()
        runner._emit_gateway_activity = MagicMock(return_value=None)
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = SessionSource(platform=Platform.TELEGRAM, user_id="12345", chat_id="67890")
        todos = [{"id": "inspect", "content": "Inspect current state", "status": "in_progress"}]

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()

            def _run_conversation(**_kwargs):
                MockAgent.call_args.kwargs["step_callback"](
                    1,
                    [{"name": "todo", "result": {"todos": todos}}],
                )
                raise asyncio.CancelledError

            mock_agent.run_conversation.side_effect = _run_conversation
            MockAgent.return_value = mock_agent
            with pytest.raises(asyncio.CancelledError):
                await runner._run_background_task("say hello", source, "bg_test")

        terminal = next(
            call.kwargs
            for call in runner._emit_gateway_activity.call_args_list
            if call.kwargs["phase"] == "cancelled"
        )
        assert terminal["task_items"] == todos

    @pytest.mark.asyncio
    async def test_failed_background_task_carries_latest_todo_snapshot(self):
        runner = _make_runner()
        runner._emit_gateway_activity = MagicMock(return_value=None)
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = SessionSource(platform=Platform.TELEGRAM, user_id="12345", chat_id="67890")
        todos = [{"id": "inspect", "content": "Inspect current state", "status": "in_progress"}]

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()

            def _run_conversation(**_kwargs):
                MockAgent.call_args.kwargs["step_callback"](
                    1,
                    [{"name": "todo", "result": {"todos": todos}}],
                )
                raise RuntimeError("boom")

            mock_agent.run_conversation.side_effect = _run_conversation
            MockAgent.return_value = mock_agent
            await runner._run_background_task("say hello", source, "bg_test")

        terminal = next(
            call.kwargs
            for call in runner._emit_gateway_activity.call_args_list
            if call.kwargs["phase"] == "failed"
        )
        assert terminal["task_items"] == todos

    @pytest.mark.asyncio
    async def test_unsuccessful_result_emits_failed_lifecycle(self):
        """A returned failure must not be represented as a completed task card."""
        runner = _make_runner()
        runner._emit_gateway_activity = MagicMock(return_value=None)
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        mock_adapter.extract_media = MagicMock(return_value=([], "Error: failed"))
        mock_adapter.extract_images = MagicMock(return_value=([], "Error: failed"))
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )
        mock_result = {
            "final_response": "Error: failed",
            "messages": [],
            "failed": True,
            "error": "failed",
        }

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("run_agent.AIAgent") as MockAgent:
            mock_agent_instance = MagicMock()
            mock_agent_instance.run_conversation.return_value = mock_result
            MockAgent.return_value = mock_agent_instance

            await runner._run_background_task("fail", source, "bg_test")

        lifecycle = runner._emit_gateway_activity.call_args.kwargs
        assert lifecycle["phase"] == "failed"
        assert lifecycle["status"] == "failed"
        assert lifecycle["terminal"] is True

    @pytest.mark.asyncio
    async def test_cancelled_task_emits_background_cancellation(self):
        runner = _make_runner()
        runner._emit_gateway_activity = MagicMock(return_value=None)
        adapter = AsyncMock()
        adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = adapter
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            user_id="user-1",
        )

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value={"api_key": "test-key"},
        ), patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.run_conversation.side_effect = asyncio.CancelledError
            MockAgent.return_value = mock_agent
            with pytest.raises(asyncio.CancelledError):
                await runner._run_background_task("cancel me", source, "bg-cancel")

        lifecycle = runner._emit_gateway_activity.call_args.kwargs
        assert lifecycle["phase"] == "cancelled"
        assert lifecycle["status"] == "cancelled"
        assert lifecycle["terminal"] is True



    @pytest.mark.asyncio
    async def test_startup_reconciliation_settles_and_clears_interrupted_task(self, monkeypatch, tmp_path):
        from gateway import run as gateway_run
        from gateway.background_task_ledger import BackgroundTaskLedger

        runner = _make_runner()
        monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)
        ledger = BackgroundTaskLedger(tmp_path / "gateway")
        ledger.register(
            "bg_recover",
            {
                "platform": Platform.TELEGRAM.value,
                "chat_id": "chat-1",
                "user_id": "user-1",
                "chat_type": "group",
                "thread_id": "thread-1",
                "session_key": "session-1",
                "event_message_id": "42",
                "task_items": [
                    {"id": "done", "content": "Already done", "status": "done"},
                    {"id": "pending", "content": "Still pending", "status": "pending"},
                ],
            },
        )
        events = []
        loop = asyncio.get_running_loop()

        def emit(**kwargs):
            events.append(kwargs)
            acknowledgement = loop.create_future()
            acknowledgement.set_result(True)
            return acknowledgement

        runner._emit_gateway_activity = emit
        await runner._reconcile_interrupted_background_tasks()
        assert len(events) == 1
        event = events[0]
        assert event["kind"] == "background"
        assert event["task_id"] == "bg_recover"
        assert event["phase"] == event["status"] == "cancelled"
        assert event["terminal"] is True
        assert event["event_message_id"] == "42"
        items = {item["id"]: item for item in event["task_items"]}
        assert items["done"]["status"] == "done"
        assert items["pending"]["status"] == "failed"
        assert items["gateway-interrupted"]["status"] == "failed"
        assert ledger.active_records() == {}

        await runner._reconcile_interrupted_background_tasks()
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_startup_reconciliation_terminalizes_only_abandoned_foreground_cards(
        self, monkeypatch, tmp_path
    ):
        import json
        from gateway import run as gateway_run

        runner = _make_runner()
        monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)
        cards_dir = tmp_path / "plugins" / "task-card" / "cards"
        cards_dir.mkdir(parents=True)
        records = {
            "stale-foreground.json": {
                "binding": "foreground:fg_stale",
                "platform": Platform.TELEGRAM.value,
                "chat_id": "-100",
                "thread_id": "thread-1",
                "session_key": "session-1",
                "generation": "foreground-generation",
                "revision": 4,
                "terminal": False,
                "items": [{"item_id": "done", "label": "Already done", "status": "complete"}],
            },
            "completed-foreground.json": {
                "binding": "foreground:fg_completed",
                "platform": Platform.TELEGRAM.value,
                "chat_id": "chat-1",
                "terminal": True,
            },
            "background.json": {
                "binding": "background:bg_active",
                "platform": Platform.TELEGRAM.value,
                "chat_id": "chat-1",
                "terminal": False,
            },
        }
        for name, record in records.items():
            (cards_dir / name).write_text(json.dumps(record), encoding="utf-8")

        acknowledgement = asyncio.get_running_loop().create_future()
        acknowledgement.set_result(True)
        runner._emit_gateway_activity = MagicMock(return_value=acknowledgement)

        await runner._reconcile_interrupted_background_tasks()

        runner._emit_gateway_activity.assert_called_once()
        event = runner._emit_gateway_activity.call_args.kwargs
        assert event["kind"] == "foreground"
        assert event["task_id"] == "fg_stale"
        assert event["phase"] == event["status"] == "cancelled"
        assert event["terminal"] is True
        assert event["source"].chat_type == "group"
        assert event["task_items"] == [{"id": "done", "content": "Already done", "status": "completed"}]
        assert runner._task_card_lifecycles["foreground:fg_stale"] == {
            "generation": "foreground-generation",
            "revision": 4,
        }

    @pytest.mark.asyncio
    async def test_foreground_reconciliation_preserves_an_empty_checklist(self, monkeypatch, tmp_path):
        import json
        from gateway import run as gateway_run

        runner = _make_runner()
        monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)
        cards_dir = tmp_path / "plugins" / "task-card" / "cards"
        cards_dir.mkdir(parents=True)
        path = cards_dir / "stale-empty.json"
        path.write_text(
            json.dumps(
                {
                    "binding": "foreground:fg_empty",
                    "platform": Platform.TELEGRAM.value,
                    "chat_id": "chat-1",
                    "generation": "foreground-generation",
                    "revision": 2,
                    "terminal": False,
                    "items": [],
                }
            ),
            encoding="utf-8",
        )
        acknowledgement = asyncio.get_running_loop().create_future()
        acknowledgement.set_result(True)
        runner._emit_gateway_activity = MagicMock(return_value=acknowledgement)

        await runner._reconcile_interrupted_foreground_cards(tmp_path, None)

        assert runner._emit_gateway_activity.call_args.kwargs["task_items"] == []

    @pytest.mark.asyncio
    async def test_reconciliation_retains_false_acknowledgement_and_discards_malformed_record(
        self, monkeypatch, tmp_path
    ):
        from gateway import run as gateway_run
        from gateway.background_task_ledger import BackgroundTaskLedger

        runner = _make_runner()
        monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)
        ledger = BackgroundTaskLedger(tmp_path / "gateway")
        ledger.register(
            "bg_retain",
            {"platform": Platform.TELEGRAM.value, "chat_id": "chat-1"},
        )
        ledger.register("bg_malformed", {"platform": Platform.TELEGRAM.value})
        acknowledgement = asyncio.get_running_loop().create_future()
        acknowledgement.set_result(False)
        runner._emit_gateway_activity = MagicMock(return_value=acknowledgement)

        await runner._reconcile_interrupted_background_tasks()

        assert set(ledger.active_records()) == {"bg_retain"}
        runner._emit_gateway_activity.assert_called_once()
        assert runner._emit_gateway_activity.call_args.kwargs["task_id"] == "bg_retain"

    @pytest.mark.asyncio
    async def test_reconciliation_isolates_mixed_terminal_acknowledgements(
        self, monkeypatch, tmp_path
    ):
        from gateway import run as gateway_run
        from gateway.background_task_ledger import BackgroundTaskLedger

        runner = _make_runner()
        monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)
        ledger = BackgroundTaskLedger(tmp_path / "gateway")
        for task_id in ("bg_clear", "bg_retain"):
            ledger.register(
                task_id,
                {"platform": Platform.TELEGRAM.value, "chat_id": task_id},
            )

        loop = asyncio.get_running_loop()

        def emit(**kwargs):
            acknowledgement = loop.create_future()
            acknowledgement.set_result(kwargs["task_id"] == "bg_clear")
            return acknowledgement

        runner._emit_gateway_activity = MagicMock(side_effect=emit)
        await runner._reconcile_interrupted_background_tasks()

        assert set(ledger.active_records()) == {"bg_retain"}
        assert {
            call.kwargs["task_id"] for call in runner._emit_gateway_activity.call_args_list
        } == {"bg_clear", "bg_retain"}

    @pytest.mark.asyncio
    async def test_reconciliation_awaits_terminal_publications_concurrently(self, monkeypatch, tmp_path):
        from gateway import run as gateway_run
        from gateway.background_task_ledger import BackgroundTaskLedger

        runner = _make_runner()
        monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)
        ledger = BackgroundTaskLedger(tmp_path / "gateway")
        for task_id in ("bg_one", "bg_two"):
            ledger.register(task_id, {"platform": Platform.TELEGRAM.value, "chat_id": task_id})

        release = asyncio.Event()
        both_awaiting = asyncio.Event()
        awaiting = set()

        async def await_publication(acknowledgement):
            awaiting.add(acknowledgement)
            if len(awaiting) == 2:
                both_awaiting.set()
            await release.wait()
            return True

        runner._emit_gateway_activity = MagicMock(side_effect=lambda **kwargs: kwargs["task_id"])
        runner._await_terminal_card_publication = AsyncMock(side_effect=await_publication)
        reconciliation = asyncio.create_task(runner._reconcile_interrupted_background_tasks())
        try:
            await asyncio.wait_for(both_awaiting.wait(), timeout=0.1)
            assert awaiting == {"bg_one", "bg_two"}
        finally:
            release.set()
            await reconciliation
        assert ledger.active_records() == {}

    @pytest.mark.asyncio
    async def test_background_ledger_uses_routed_profile_home(self, monkeypatch, tmp_path):
        from gateway import run as gateway_run
        from gateway.background_task_ledger import BackgroundTaskLedger

        runner = _make_runner()
        runner.config = MagicMock(multiplex_profiles=True)
        profile_home = tmp_path / "profiles" / "reviewer"
        runner._resolve_profile_home_for_source = MagicMock(return_value=profile_home)
        monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)
        adapter = AsyncMock()
        adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = adapter
        runner._adapter_for_source = MagicMock(return_value=adapter)
        runner._resolve_session_agent_runtime = MagicMock(return_value=("test-model", {}))
        acknowledgement = asyncio.get_running_loop().create_future()
        acknowledgement.set_result(False)
        runner._emit_gateway_activity = MagicMock(return_value=acknowledgement)

        await runner._run_background_task(
            "fail",
            SessionSource(platform=Platform.TELEGRAM, user_id="user", chat_id="chat", profile="reviewer"),
            "bg_profile",
        )

        assert "bg_profile" in BackgroundTaskLedger(profile_home / "gateway").active_records()
        assert BackgroundTaskLedger(tmp_path / "gateway").active_records() == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("acknowledgement", [True, False, "timeout"])
    async def test_failed_background_terminal_clears_ledger_only_after_acknowledgement(
        self, monkeypatch, tmp_path, acknowledgement
    ):
        from gateway import run as gateway_run
        from gateway.background_task_ledger import BackgroundTaskLedger

        runner = _make_runner()
        monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)
        adapter = AsyncMock()
        adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = adapter
        source = SessionSource(platform=Platform.TELEGRAM, user_id="user", chat_id="chat")

        if acknowledgement == "timeout":
            runner._emit_gateway_activity = MagicMock(return_value=object())
            runner._await_terminal_card_publication = AsyncMock(return_value=False)
        else:
            future = asyncio.get_running_loop().create_future()
            future.set_result(acknowledgement)
            runner._emit_gateway_activity = MagicMock(return_value=future)

        runner._resolve_session_agent_runtime = MagicMock(return_value=("test-model", {}))
        await runner._run_background_task("fail", source, "bg_failed")

        records = BackgroundTaskLedger(tmp_path / "gateway").active_records()
        assert ("bg_failed" not in records) is (acknowledgement is True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("acknowledgement", [True, False, "timeout"])
    async def test_completed_background_terminal_clears_ledger_only_after_acknowledgement(
        self, monkeypatch, tmp_path, acknowledgement
    ):
        from gateway import run as gateway_run
        from gateway.background_task_ledger import BackgroundTaskLedger

        runner = _make_runner()
        monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)
        adapter = AsyncMock()
        adapter.send = AsyncMock()
        adapter.extract_media = MagicMock(return_value=([], "done"))
        adapter.extract_images = MagicMock(return_value=([], "done"))
        runner.adapters[Platform.TELEGRAM] = adapter
        source = SessionSource(platform=Platform.TELEGRAM, user_id="user", chat_id="chat")

        if acknowledgement == "timeout":
            runner._emit_gateway_activity = MagicMock(return_value=object())
            runner._await_terminal_card_publication = AsyncMock(return_value=False)
        else:
            future = asyncio.get_running_loop().create_future()
            future.set_result(acknowledgement)
            runner._emit_gateway_activity = MagicMock(return_value=future)

        runner._resolve_session_agent_runtime = MagicMock(
            return_value=("test-model", {"api_key": "test-key"})
        )
        runner._resolve_session_reasoning_config = MagicMock(return_value=None)
        runner._load_service_tier = MagicMock(return_value=None)
        runner._resolve_turn_agent_config = MagicMock(
            return_value={"model": "test-model", "runtime": {}, "request_overrides": None}
        )
        runner._run_in_executor_with_context = AsyncMock(
            return_value={"final_response": "done", "messages": []}
        )
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
        await runner._run_background_task("complete", source, "bg_completed")

        records = BackgroundTaskLedger(tmp_path / "gateway").active_records()
        assert ("bg_completed" not in records) is (acknowledgement is True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("acknowledgement", [True, False, "timeout"])
    async def test_cancelled_background_terminal_clears_ledger_only_after_acknowledgement(
        self, monkeypatch, tmp_path, acknowledgement
    ):
        from gateway import run as gateway_run
        from gateway.background_task_ledger import BackgroundTaskLedger

        runner = _make_runner()
        monkeypatch.setattr(gateway_run, "get_hermes_home", lambda: tmp_path)
        adapter = AsyncMock()
        adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = adapter
        source = SessionSource(platform=Platform.TELEGRAM, user_id="user", chat_id="chat")

        if acknowledgement == "timeout":
            runner._emit_gateway_activity = MagicMock(return_value=object())
            runner._await_terminal_card_publication = AsyncMock(return_value=False)
        else:
            future = asyncio.get_running_loop().create_future()
            future.set_result(acknowledgement)
            runner._emit_gateway_activity = MagicMock(return_value=future)

        runner._resolve_session_agent_runtime = MagicMock(
            return_value=("test-model", {"api_key": "test-key"})
        )
        runner._resolve_session_reasoning_config = MagicMock(return_value=None)
        runner._load_service_tier = MagicMock(return_value=None)
        runner._resolve_turn_agent_config = MagicMock(
            return_value={"model": "test-model", "runtime": {}, "request_overrides": None}
        )
        runner._run_in_executor_with_context = AsyncMock(side_effect=asyncio.CancelledError)
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})

        with pytest.raises(asyncio.CancelledError):
            await runner._run_background_task("cancel", source, "bg_cancelled")

        records = BackgroundTaskLedger(tmp_path / "gateway").active_records()
        assert ("bg_cancelled" not in records) is (acknowledgement is True)

# ---------------------------------------------------------------------------
# /background in help and known_commands
# ---------------------------------------------------------------------------


class TestBackgroundInHelp:
    """Verify /background appears in help text and known commands."""

    @pytest.mark.asyncio
    async def test_background_in_help_output(self):
        """The /help output includes /background."""
        runner = _make_runner()
        event = _make_event(text="/help")
        result = await runner._handle_help_command(event)
        assert "/background" in result


# ---------------------------------------------------------------------------
# CLI /background command definition
# ---------------------------------------------------------------------------


class TestBackgroundInCLICommands:
    """Verify /background is registered in the CLI command system."""


    def test_background_autocompletes(self):
        """The /background command appears in autocomplete results."""
        pytest.importorskip("prompt_toolkit")
        from hermes_cli.commands import SlashCommandCompleter
        from prompt_toolkit.document import Document

        completer = SlashCommandCompleter()
        doc = Document("backgro")  # Partial match
        completions = list(completer.get_completions(doc, None))
        # Text doesn't start with / so no completions
        assert len(completions) == 0

        doc = Document("/backgro")  # With slash prefix
        completions = list(completer.get_completions(doc, None))
        cmd_displays = [str(c.display) for c in completions]
        assert any("/background" in d for d in cmd_displays)
