"""Tests for the phase-1 opportunity-routing helpers.

These cover the phase-1 requirements directly:
- local brainstorming stays local
- validation-style prompts route to opportunity-radar
- canonical CLI shape + payload construction
- JSON parsing / retry-once failure modes
- the closest real in-flight guard available in phase 1
- a small end-to-end run_conversation short-circuit
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent import opportunity_routing as routing
from run_agent import AIAgent


def _make_agent(max_iterations: int = 10) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=max_iterations,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._fallback_chain = []
    return agent


def _specialist_json(*, mode: str = "single_idea_vetting") -> str:
    return json.dumps(
        {
            "mode": mode,
            "summary": "The concept is plausible but needs a stronger wedge.",
            "recommendation": "pursue",
            "confidence": "high",
            "findings": [
                {
                    "name": "Clear buyer",
                    "why_now": "The customer pain is specific and recurring.",
                },
                {
                    "name": "Fast validation",
                    "why_now": "A cheap pilot can test demand quickly.",
                },
            ],
            "next_best_test": "Run 5 customer interviews and offer a paid pilot.",
            "caveats": ["Assumes you can reach buyers cheaply."],
            "follow_up_questions": ["Who is the first customer segment?"]
        }
    )


class _FakeProcess:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0, timeout_once: bool = False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timeout_once = timeout_once
        self.communicate_calls = 0
        self.killed = False
        self.terminated = False
        self.wait_calls = 0
        self.pid = 4321
        self.cmd = ()

    def communicate(self, timeout=None):
        self.communicate_calls += 1
        if self.timeout_once and self.communicate_calls == 1:
            raise routing.subprocess.TimeoutExpired(self.cmd or ("hermes",), timeout)
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        return self.returncode


class _FakePopenFactory:
    def __init__(self, *processes: _FakeProcess):
        self.processes = list(processes)
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((tuple(cmd), dict(kwargs)))
        proc = self.processes.pop(0)
        proc.cmd = tuple(cmd)
        return proc


def test_brainstorming_stays_local():
    assert not routing.should_route_opportunity_request(
        "Brainstorm 5 startup ideas for local cafés."
    )
    assert routing.maybe_route_opportunity_request(
        _make_agent(),
        user_message="Brainstorm 5 startup ideas for local cafés.",
        original_user_message="Brainstorm 5 startup ideas for local cafés.",
        source_platform="cli",
    ) is None


def test_validation_prompt_routes_and_classifies_mode():
    prompt = "Vet this idea: B2B workflow audits for small venues."
    assert routing.should_route_opportunity_request(prompt) is True
    assert routing.classify_opportunity_mode(prompt) == "single_idea_vetting"
    assert routing.classify_opportunity_mode("Compare these two opportunities.") == "comparison"
    assert routing.classify_opportunity_mode("Give me 5 opportunities under $500.") == "scan"
    assert routing.classify_opportunity_mode("Find me 5 B2B opportunities under $1,000.") == "scan"


def test_broad_business_terms_do_not_route_without_opportunity_context():
    assert routing.should_route_opportunity_request(
        "Help me set pricing for my existing web app."
    ) is False
    assert routing.should_route_opportunity_request(
        "Is it feasible to add OAuth to Hermes?"
    ) is False
    assert routing.should_route_opportunity_request(
        "Brainstorm ideas only. Do not evaluate or rank them yet."
    ) is False


def test_dispatch_payload_does_not_route_again(monkeypatch):
    payload = routing.build_dispatch_payload(
        original_user_message="Find me 5 B2B opportunities I can validate in 2 weeks.",
        mode="scan",
        source_platform="telegram",
        response_style="concise_relay",
        constraints=["under $1,000", "time to revenue"],
    )

    assert routing.should_route_opportunity_request(payload) is False
    assert routing.should_route_opportunity_request(payload.replace('"dispatch_version":1', '"dispatch_version":2')) is False

    agent = SimpleNamespace(platform="cli", session_id="specialist-test")
    monkeypatch.setenv("HERMES_PROFILE", routing.OPPORTUNITY_RADAR_PROFILE)
    assert routing.maybe_route_opportunity_request(
        agent,
        user_message="Vet this idea: a workflow audit service for local venues.",
        original_user_message="Vet this idea: a workflow audit service for local venues.",
        source_platform="cli",
    ) is None


def test_dispatch_payload_preserves_original_message_and_constraints():
    payload = routing.build_dispatch_payload(
        original_user_message="Vet this idea: B2B workflow audits under $500 and within 2 weeks.",
        mode="single_idea_vetting",
        source_platform="telegram",
        response_style="concise_relay",
        constraints=["under $500", "within 2 weeks"],
    )
    data = json.loads(payload)
    assert data["dispatch_version"] == 1
    assert data["mode"] == "single_idea_vetting"
    assert data["source_platform"] == "telegram"
    assert data["response_style"] == "concise_relay"
    assert data["original_user_message"].startswith("Vet this idea:")
    assert data["constraints"] == ["under $500", "within 2 weeks"]
    assert data["strict_json_only"] is True
    assert data["requested_output_schema"]["recommendation"] == "pursue|park|reject|investigate_more"


def test_route_success_uses_canonical_cli_shape_and_renders_relay(monkeypatch):
    agent = _make_agent()
    factory = _FakePopenFactory(_FakeProcess(stdout=f"```json\n{_specialist_json()}\n```"))

    monkeypatch.setattr(routing, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(routing.subprocess, "Popen", factory)

    outcome = routing.route_opportunity_request(
        agent,
        user_message="Vet this idea: local venue workflow audits.",
        original_user_message="Vet this idea: local venue workflow audits.",
        source_platform="telegram",
    )

    assert outcome.success is True
    assert outcome.attempts == 1
    assert outcome.command[:4] == ("hermes", "-p", "opportunity-radar", "chat")
    assert outcome.command[4] == "-q"
    payload = json.loads(outcome.command[5])
    assert payload["original_user_message"] == "Vet this idea: local venue workflow audits."
    assert payload["mode"] == "single_idea_vetting"
    assert "Verdict: pursue (high confidence)" in outcome.final_response
    assert "Next-best test:" in outcome.final_response
    assert "Caveats:" in outcome.final_response
    assert factory.calls[0][1]["start_new_session"] is True


def test_route_retries_once_on_dispatch_failure_then_succeeds(monkeypatch):
    agent = _make_agent()
    factory = _FakePopenFactory(
        _FakeProcess(returncode=1, stderr="boom"),
        _FakeProcess(stdout=_specialist_json()),
    )

    monkeypatch.setattr(routing, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(routing.subprocess, "Popen", factory)

    outcome = routing.route_opportunity_request(
        agent,
        user_message="Compare these two opportunities.",
        original_user_message="Compare these two opportunities.",
        source_platform="cli",
    )

    assert outcome.success is True
    assert outcome.attempts == 2
    assert outcome.turn_exit_reason == "opportunity_route_success"
    assert len(factory.calls) == 2
    assert "retry_reason" in json.loads(factory.calls[1][0][5])


def test_route_retries_once_on_nonzero_exit_then_fails(monkeypatch):
    agent = _make_agent()
    factory = _FakePopenFactory(
        _FakeProcess(returncode=1, stderr="boom on attempt 1"),
        _FakeProcess(returncode=1, stderr="boom on attempt 2"),
    )

    monkeypatch.setattr(routing, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(routing.subprocess, "Popen", factory)

    outcome = routing.route_opportunity_request(
        agent,
        user_message="Compare these two opportunities.",
        original_user_message="Compare these two opportunities.",
        source_platform="cli",
    )

    assert outcome.success is False
    assert outcome.attempts == 2
    assert outcome.turn_exit_reason == "opportunity_route_dispatch_failed"
    assert "specialist exited 1" in outcome.final_response
    assert "failed after 2 attempt(s)" in outcome.final_response
    assert "Diagnostic ID:" in outcome.final_response
    assert len(factory.calls) == 2


def test_route_retries_once_on_malformed_json_then_fails(monkeypatch):
    agent = _make_agent()
    factory = _FakePopenFactory(
        _FakeProcess(stdout="not valid json"),
        _FakeProcess(stdout="still not valid json"),
    )

    monkeypatch.setattr(routing, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(routing.subprocess, "Popen", factory)

    outcome = routing.route_opportunity_request(
        agent,
        user_message="Vet this idea: a workflow audit service for local venues.",
        original_user_message="Vet this idea: a workflow audit service for local venues.",
        source_platform="cli",
    )

    assert outcome.success is False
    assert outcome.attempts == 2
    assert outcome.turn_exit_reason == "opportunity_route_malformed_json"
    assert "malformed JSON" in outcome.final_response
    assert "failed after 2 attempt(s)" in outcome.final_response
    assert "Diagnostic ID:" in outcome.final_response
    assert "Command:" not in outcome.final_response
    assert "Specialist output preview:" not in outcome.final_response
    assert "strict_json_only" not in outcome.final_response
    assert "not valid json" not in outcome.final_response
    assert len(factory.calls) == 2


def test_route_retries_once_on_validation_failure_then_fails(monkeypatch):
    agent = _make_agent()
    invalid_payload = json.dumps(
        {
            "mode": "comparison",
            "summary": "Looks promising but the schema is wrong.",
            "recommendation": "explore",
            "confidence": "medium",
            "findings": ["Needs more evidence"],
            "next_best_test": "Run a quick landing-page test.",
            "caveats": [],
            "follow_up_questions": [],
        }
    )
    factory = _FakePopenFactory(
        _FakeProcess(stdout=invalid_payload),
        _FakeProcess(stdout=invalid_payload),
    )

    monkeypatch.setattr(routing, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(routing.subprocess, "Popen", factory)

    outcome = routing.route_opportunity_request(
        agent,
        user_message="Compare these two opportunities.",
        original_user_message="Compare these two opportunities.",
        source_platform="cli",
    )

    assert outcome.success is False
    assert outcome.attempts == 2
    assert outcome.turn_exit_reason == "opportunity_route_validation_failed"
    assert "validation" in outcome.final_response.lower()
    assert "Diagnostic ID:" in outcome.final_response
    assert len(factory.calls) == 2


def test_route_retries_once_on_missing_required_fields_then_fails(monkeypatch):
    agent = _make_agent()
    invalid_payload = json.dumps(
        {
            "mode": "single_idea_vetting",
            "summary": "Looks plausible but the schema is incomplete.",
            "recommendation": "pursue",
            "confidence": "high",
            "findings": [],
            "caveats": [],
            "follow_up_questions": [],
        }
    )
    factory = _FakePopenFactory(
        _FakeProcess(stdout=invalid_payload),
        _FakeProcess(stdout=invalid_payload),
    )

    monkeypatch.setattr(routing, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(routing.subprocess, "Popen", factory)

    outcome = routing.route_opportunity_request(
        agent,
        user_message="Vet this idea: a workflow audit service for local venues.",
        original_user_message="Vet this idea: a workflow audit service for local venues.",
        source_platform="cli",
    )

    assert outcome.success is False
    assert outcome.attempts == 2
    assert outcome.turn_exit_reason == "opportunity_route_validation_failed"
    assert "validation" in outcome.final_response.lower()
    assert outcome.error == "specialist JSON failed validation: missing next_best_test"
    assert len(factory.calls) == 2


def test_route_cleans_up_timed_out_process_group_and_retries(monkeypatch):
    agent = _make_agent()
    first = _FakeProcess(timeout_once=True)
    second = _FakeProcess(stdout=_specialist_json())
    factory = _FakePopenFactory(first, second)
    killpg = MagicMock()

    monkeypatch.setattr(routing, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(routing.subprocess, "Popen", factory)
    monkeypatch.setattr(routing.os, "killpg", killpg)

    outcome = routing.route_opportunity_request(
        agent,
        user_message="Vet this idea: a workflow audit service for local venues.",
        original_user_message="Vet this idea: a workflow audit service for local venues.",
        source_platform="cli",
    )

    assert outcome.success is True
    assert outcome.attempts == 2
    assert first.killed is True
    assert first.wait_calls >= 1
    assert killpg.call_count == 1
    assert len(factory.calls) == 2


def test_route_lock_is_removed_after_release(monkeypatch):
    agent = _make_agent()
    key = routing._route_lock_key(agent)
    assert key not in routing._ROUTE_LOCKS
    with routing._claim_route_lock(agent):
        assert key in routing._ROUTE_LOCKS
    assert key not in routing._ROUTE_LOCKS


def test_busy_guard_returns_clear_message(monkeypatch):
    agent = _make_agent()
    key = routing._route_lock_key(agent)
    with routing._ROUTE_LOCKS_GUARD:
        lock = routing._ROUTE_LOCKS.setdefault(key, routing.threading.Lock())
    assert lock.acquire(blocking=False) is True
    try:
        outcome = routing.maybe_route_opportunity_request(
            agent,
            user_message="Vet this idea: a workflow audit service for local venues.",
            original_user_message="Vet this idea: a workflow audit service for local venues.",
            source_platform="cli",
        )
    finally:
        lock.release()

    assert outcome is not None
    assert outcome.success is False
    assert "already in progress" in outcome.final_response
    assert outcome.turn_exit_reason == "opportunity_route_busy"


def test_run_conversation_no_longer_short_circuits_on_validation_prompt():
    agent = _make_agent(max_iterations=1)
    agent.client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="Keeping this local.", tool_calls=[]),
                finish_reason="stop",
            )
        ],
        usage=None,
    )

    result = agent.run_conversation("Vet this idea: local venue workflow audits.")

    assert result["final_response"] == "Keeping this local."
    assert result["api_calls"] == 1
    assert result["failed"] is False
    assert agent.client.chat.completions.create.call_count == 1
