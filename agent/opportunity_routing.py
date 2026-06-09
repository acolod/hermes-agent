"""Synchronous opportunity-routing helpers for phase 2.

The default/Kimi front door can use this module to keep loose brainstorming
local while routing validation-style opportunity prompts to the
``opportunity-radar`` specialist profile via the canonical Hermes CLI
shape. Phase 2 hardens that flow with a direct slash-command path, more
conservative fallback routing, cleaner failure text, and timeout / lock
cleanup.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

try:
    from hermes_cli.kanban_db import _resolve_hermes_argv
except Exception:  # pragma: no cover - import fallback for unusual setups
    def _resolve_hermes_argv() -> list[str]:
        return [sys.executable, "-m", "hermes_cli.main"]


OPPORTUNITY_RADAR_PROFILE = "opportunity-radar"

# Routing heuristics intentionally err on the side of staying local unless the
# request clearly asks for validation, narrowing, comparison, or screening.
_ROUTE_MARKERS = (
    "validate this",
    "vet this",
    "screen this",
    "compare these",
    "compare the",
    "compare my",
    "shortlist",
    "rank these",
    "which should i pursue",
    "which idea should i pursue",
    "pursue or park",
    "pursue / park",
    "pursue, park, or reject",
    "park or reject",
    "reject this",
    "cheapest validation",
    "validation test",
    "market validation",
    "market test",
    "opportunity screening",
    "should i pursue",
    "should we pursue",
)

_SCAN_CONTEXT_MARKERS = (
    "find me 3",
    "find me 4",
    "find me 5",
    "give me 3",
    "give me 4",
    "give me 5",
    "top 3",
    "top 4",
    "top 5",
    "list 3",
    "list 4",
    "list 5",
    "what opportunities",
    "what businesses",
    "what side hustles",
    "find opportunities",
    "opportunities to",
    "ideas to test",
)

_BROAD_BUSINESS_MARKERS = (
    "pricing",
    "feasible",
    "competition",
    "wedge",
    "economics",
    "unit economics",
    "startup cost",
    "time to revenue",
    "time-to-revenue",
    "viable",
    "worth pursuing",
    "worth building",
)

_OPPORTUNITY_CONTEXT_MARKERS = (
    "opportunity",
    "opportunities",
    "idea",
    "ideas",
    "business",
    "businesses",
    "startup",
    "side hustle",
    "side hustles",
    "b2b",
    "b2c",
    "validation",
    "validate",
    "vet",
    "screen",
    "compare",
    "shortlist",
    "rank",
    "pursue",
    "park",
    "reject",
)

_COMPARISON_MARKERS = (
    "compare",
    "which is better",
    "which one is better",
    "which should i choose",
    "which should i pick",
    "best option",
    "best idea",
    "rank",
    "versus",
    "vs",
)

_LOCAL_ONLY_HINTS = (
    "brainstorm",
    "brainstorming",
    "idea generation",
    "give me ideas",
    "suggest ideas",
    "loose ideas",
    "copywriting",
    "implementation help",
    "write code",
)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_CONSTRAINT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|[;\n]+")

_ALLOWED_RECOMMENDATIONS = {"pursue", "park", "reject", "investigate_more"}
_ALLOWED_CONFIDENCE = {"low", "medium", "high"}
_ALLOWED_MODES = {"scan", "single_idea_vetting", "comparison"}


def _normalize(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip().lower()


def _has_any_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _looks_like_dispatch_payload(text: str) -> bool:
    """Return True when text is the normalized specialist handoff payload.

    The default profile sends this JSON to the opportunity-radar profile. The
    specialist must handle it directly; routing it again recursively spawns
    another specialist process with a larger escaped JSON payload each time.
    """

    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    try:
        payload = json.loads(stripped, strict=False)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("strict_json_only") is not True:
        return False
    if payload.get("dispatch_version") is not None:
        return True
    return bool(
        payload.get("requested_output_schema")
        and payload.get("original_user_message")
        and payload.get("source_platform")
    )


def _active_profile_name(agent: Any) -> str:
    for attr in ("profile", "profile_name", "active_profile"):
        value = getattr(agent, attr, None)
        if value:
            return str(value)
    return os.environ.get("HERMES_PROFILE", "")


def should_route_opportunity_request(user_message: str) -> bool:
    """Return True only for validation-style opportunity prompts.

    Loose brainstorming stays local by default. The heuristic only routes when
    the text clearly asks for evaluation, narrowing, comparison, or screening.
    """

    text = _normalize(user_message)
    if not text:
        return False

    if _looks_like_dispatch_payload(user_message):
        return False

    has_clear_route = _has_any_marker(text, _ROUTE_MARKERS) or _has_any_marker(
        text, _SCAN_CONTEXT_MARKERS
    ) or _has_any_marker(text, _COMPARISON_MARKERS)
    if _has_any_marker(text, _LOCAL_ONLY_HINTS) and not _has_any_marker(text, _ROUTE_MARKERS):
        return False

    if has_clear_route:
        return True

    return _has_any_marker(text, _BROAD_BUSINESS_MARKERS) and _has_any_marker(
        text, _OPPORTUNITY_CONTEXT_MARKERS
    )


def classify_opportunity_mode(user_message: str) -> str:
    """Classify a routed request for the specialist payload."""

    text = _normalize(user_message)
    if any(marker in text for marker in _COMPARISON_MARKERS):
        return "comparison"
    if any(marker in text for marker in _SCAN_CONTEXT_MARKERS):
        return "scan"
    return "single_idea_vetting"


def extract_explicit_constraints(user_message: str) -> list[str]:
    """Extract only constraints that are stated explicitly in the prompt."""

    if not isinstance(user_message, str):
        return []

    text = user_message.strip()
    if not text:
        return []

    constraints: list[str] = []
    for clause in _CONSTRAINT_SPLIT_RE.split(text):
        cleaned = clause.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if any(
            marker in lowered
            for marker in (
                "under $",
                "within ",
                "must ",
                "must be",
                "need to",
                "needs to",
                "can't",
                "cannot",
                "without ",
                "no ",
                "budget",
                "targeting ",
                "for small",
                "for b2b",
                "for b2c",
                "in the us",
                "in europe",
                "in canada",
                "fast validation",
                "time to revenue",
                "time-to-revenue",
                "startup cost",
            )
        ):
            if cleaned not in constraints:
                constraints.append(cleaned)
    return constraints[:8]


def build_dispatch_payload(
    *,
    original_user_message: str,
    mode: str,
    source_platform: str,
    response_style: str,
    constraints: list[str],
    retry_reason: Optional[str] = None,
) -> str:
    """Build the normalized wrapper passed to the specialist profile."""

    if mode not in _ALLOWED_MODES:
        raise ValueError(f"invalid opportunity mode: {mode}")

    payload: dict[str, Any] = {
        "dispatch_version": 1,
        "mode": mode,
        "source_platform": source_platform,
        "response_style": response_style,
        "constraints": constraints,
        "original_user_message": original_user_message,
        "requested_output_schema": {
            "mode": "scan|single_idea_vetting|comparison",
            "summary": "string",
            "recommendation": "pursue|park|reject|investigate_more",
            "confidence": "low|medium|high",
            "findings": [],
            "next_best_test": "string",
            "caveats": [],
            "follow_up_questions": [],
        },
        "strict_json_only": True,
    }
    if retry_reason:
        payload["retry_reason"] = retry_reason

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class OpportunityRoutingOutcome:
    routed: bool
    success: bool
    final_response: str
    mode: str
    turn_exit_reason: str
    payload: str = ""
    command: tuple[str, ...] = ()
    attempts: int = 0
    raw_output: str = ""
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class OpportunityRoutingError(RuntimeError):
    """Base class for routing/dispatch failures."""


class OpportunityRoutingBusyError(OpportunityRoutingError):
    """Raised when the process-local in-flight guard is already held."""


_ROUTE_LOCKS: dict[str, threading.Lock] = {}
_ROUTE_LOCKS_GUARD = threading.Lock()


def _route_lock_key(agent: Any) -> str:
    for attr in ("_gateway_session_key", "gateway_session_key", "session_id"):
        value = getattr(agent, attr, None)
        if value:
            return str(value)

    platform = getattr(agent, "platform", None) or "cli"
    chat_id = getattr(agent, "chat_id", None)
    thread_id = getattr(agent, "thread_id", None)
    if chat_id or thread_id:
        return f"{platform}:{chat_id or '-'}:{thread_id or '-'}"

    return f"{platform}:{id(agent)}"


@contextlib.contextmanager
def _claim_route_lock(agent: Any) -> Iterator[None]:
    key = _route_lock_key(agent)
    with _ROUTE_LOCKS_GUARD:
        lock = _ROUTE_LOCKS.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise OpportunityRoutingBusyError(
            "An opportunity-routing validation is already in progress for this chat/thread. "
            "Please wait for it to finish before sending another validation request."
        )
    try:
        yield
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass
        finally:
            with _ROUTE_LOCKS_GUARD:
                if _ROUTE_LOCKS.get(key) is lock and not lock.locked():
                    _ROUTE_LOCKS.pop(key, None)


def _extract_json_blob(raw: str) -> Optional[dict[str, Any]]:
    if not raw:
        return None

    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None

    candidate = stripped[first : last + 1]
    try:
        parsed = json.loads(candidate, strict=False)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _coerce_text_block(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("summary", "name", "title", "reason", "why_now", "detail", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _normalize_findings(findings: Any) -> list[Any]:
    if findings is None:
        return []
    if isinstance(findings, list):
        return findings
    return [findings]


def _validate_specialist_json(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("specialist output is not an object")

    normalized = dict(data)

    mode = normalized.get("mode")
    if mode not in _ALLOWED_MODES:
        raise ValueError(f"invalid mode: {mode!r}")

    summary = normalized.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("missing summary")
    normalized["summary"] = summary.strip()

    recommendation = normalized.get("recommendation")
    if recommendation not in _ALLOWED_RECOMMENDATIONS:
        raise ValueError(f"invalid recommendation: {recommendation!r}")

    confidence = normalized.get("confidence")
    if confidence not in _ALLOWED_CONFIDENCE:
        raise ValueError(f"invalid confidence: {confidence!r}")

    next_best_test = normalized.get("next_best_test")
    if not isinstance(next_best_test, str) or not next_best_test.strip():
        raise ValueError("missing next_best_test")
    normalized["next_best_test"] = next_best_test.strip()

    normalized["findings"] = _normalize_findings(normalized.get("findings"))
    normalized["caveats"] = _normalize_findings(normalized.get("caveats"))
    normalized["follow_up_questions"] = _normalize_findings(
        normalized.get("follow_up_questions")
    )

    return normalized


def _render_relay_message(data: dict[str, Any]) -> str:
    recommendation = data["recommendation"]
    confidence = data["confidence"]
    summary = data["summary"]
    findings = data.get("findings") or []
    caveats = data.get("caveats") or []
    next_best_test = data["next_best_test"]
    follow_up_questions = data.get("follow_up_questions") or []

    lines = [
        f"Verdict: {recommendation} ({confidence} confidence)",
        f"Summary: {summary}",
    ]

    strongest: list[str] = []
    for item in findings[:3]:
        text = _coerce_text_block(item)
        if text:
            strongest.append(text)

    if strongest:
        lines.append("Strongest reasons:")
        for item in strongest:
            lines.append(f"- {item}")

    lines.append(f"Next-best test: {next_best_test}")

    if caveats:
        lines.append("Caveats:")
        for item in caveats[:5]:
            text = _coerce_text_block(item)
            if text:
                lines.append(f"- {text}")

    if follow_up_questions:
        lines.append("Follow-up questions:")
        for item in follow_up_questions[:5]:
            text = _coerce_text_block(item)
            if text:
                lines.append(f"- {text}")

    return "\n".join(lines)


def _resolve_command() -> list[str]:
    argv = list(_resolve_hermes_argv())
    if not argv:
        return [sys.executable, "-m", "hermes_cli.main"]
    return argv


def _specialist_timeout_seconds() -> int:
    raw = os.environ.get("HERMES_OPPORTUNITY_RADAR_TIMEOUT_SECONDS", "120").strip()
    try:
        timeout = int(raw)
    except ValueError:
        return 120
    return max(30, min(timeout, 900))


def _terminate_specialist_process(proc: subprocess.Popen[str]) -> None:
    """Best-effort cleanup for a timed-out specialist subprocess."""

    with contextlib.suppress(Exception):
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=1)


def _run_specialist_once(
    *,
    payload: str,
    timeout_seconds: int,
) -> tuple[subprocess.CompletedProcess[str], tuple[str, ...]]:
    cmd = tuple(_resolve_command() + ["-p", OPPORTUNITY_RADAR_PROFILE, "chat", "-q", payload, "--quiet"])
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_specialist_process(proc)
        raise
    completed = subprocess.CompletedProcess(cmd, proc.returncode or 0, stdout, stderr)
    return completed, cmd


def _format_failure_message(
    *,
    reason: str,
    attempts: int,
) -> str:
    diagnostic_id = f"oppr-{uuid.uuid4().hex[:8]}"
    lines = [
        "I tried to route this opportunity-validation request to the opportunity-radar specialist,",
        f"but it failed after {attempts} attempt(s).",
        f"Reason: {reason}",
        f"Diagnostic ID: {diagnostic_id}",
        "Please retry the request or rephrase it if you want a different validation pass.",
    ]
    return "\n".join(lines)


def route_opportunity_request(
    agent: Any,
    *,
    user_message: str,
    original_user_message: str,
    source_platform: Optional[str] = None,
) -> OpportunityRoutingOutcome:
    """Route a validation-style opportunity request synchronously.

    Raises only for truly unexpected failures; user-facing dispatch issues are
    converted into a visible failure response so the turn can complete cleanly.
    """

    mode = classify_opportunity_mode(user_message)
    constraints = extract_explicit_constraints(original_user_message)
    response_style = "concise_relay"
    payload = build_dispatch_payload(
        original_user_message=original_user_message,
        mode=mode,
        source_platform=(source_platform or getattr(agent, "platform", None) or "cli"),
        response_style=response_style,
        constraints=constraints,
    )

    timeout_seconds = _specialist_timeout_seconds()
    attempts = 0
    raw_output = ""
    command: tuple[str, ...] = ()
    last_error = ""

    with _claim_route_lock(agent):
        for attempt in (1, 2):
            attempts = attempt
            retry_reason = None if attempt == 1 else last_error or "retry after malformed JSON"
            if attempt > 1:
                payload = build_dispatch_payload(
                    original_user_message=original_user_message,
                    mode=mode,
                    source_platform=(source_platform or getattr(agent, "platform", None) or "cli"),
                    response_style=response_style,
                    constraints=constraints,
                    retry_reason=retry_reason,
                )
            try:
                completed, command = _run_specialist_once(
                    payload=payload,
                    timeout_seconds=timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                if isinstance(exc, subprocess.TimeoutExpired):
                    last_error = f"dispatch failed: {type(exc).__name__}: timed out after {exc.timeout}s"
                else:
                    last_error = f"dispatch failed: {type(exc).__name__}: {exc}"
                if attempt == 1:
                    continue
                return OpportunityRoutingOutcome(
                    routed=True,
                    success=False,
                    final_response=_format_failure_message(
                        reason=last_error,
                        attempts=attempts,
                    ),
                    mode=mode,
                    turn_exit_reason="opportunity_route_dispatch_failed",
                    payload=payload,
                    command=command,
                    attempts=attempts,
                    raw_output=raw_output,
                    error=last_error,
                )

            raw_output = (completed.stdout or "").strip()
            stderr = (completed.stderr or "").strip()
            if completed.returncode != 0:
                last_error = (
                    f"specialist exited {completed.returncode}"
                    + (f": {stderr.splitlines()[0][:220]}" if stderr else "")
                )
                if attempt == 1:
                    continue
                return OpportunityRoutingOutcome(
                    routed=True,
                    success=False,
                    final_response=_format_failure_message(
                        reason=last_error,
                        attempts=attempts,
                    ),
                    mode=mode,
                    turn_exit_reason="opportunity_route_dispatch_failed",
                    payload=payload,
                    command=command,
                    attempts=attempts,
                    raw_output=raw_output,
                    error=last_error,
                )

            parsed = _extract_json_blob(raw_output)
            if parsed is None:
                last_error = "specialist returned malformed JSON"
                if attempt == 1:
                    continue
                return OpportunityRoutingOutcome(
                    routed=True,
                    success=False,
                    final_response=_format_failure_message(
                        reason=last_error,
                        attempts=attempts,
                    ),
                    mode=mode,
                    turn_exit_reason="opportunity_route_malformed_json",
                    payload=payload,
                    command=command,
                    attempts=attempts,
                    raw_output=raw_output,
                    error=last_error,
                )

            try:
                validated = _validate_specialist_json(parsed)
            except ValueError as exc:
                last_error = f"specialist JSON failed validation: {exc}"
                if attempt == 1:
                    continue
                return OpportunityRoutingOutcome(
                    routed=True,
                    success=False,
                    final_response=_format_failure_message(
                        reason=last_error,
                        attempts=attempts,
                    ),
                    mode=mode,
                    turn_exit_reason="opportunity_route_validation_failed",
                    payload=payload,
                    command=command,
                    attempts=attempts,
                    raw_output=raw_output,
                    error=last_error,
                )

            final_response = _render_relay_message(validated)
            return OpportunityRoutingOutcome(
                routed=True,
                success=True,
                final_response=final_response,
                mode=mode,
                turn_exit_reason="opportunity_route_success",
                payload=payload,
                command=command,
                attempts=attempts,
                raw_output=raw_output,
                data=validated,
            )

    return OpportunityRoutingOutcome(
        routed=True,
        success=False,
        final_response=_format_failure_message(
            reason=last_error or "routing guard failed unexpectedly",
            attempts=attempts or 1,
        ),
        mode=mode,
        turn_exit_reason="opportunity_route_failed",
        payload=payload,
        command=command,
        attempts=attempts or 1,
        raw_output=raw_output,
        error=last_error or "routing guard failed unexpectedly",
    )


def maybe_route_opportunity_request(
    agent: Any,
    *,
    user_message: str,
    original_user_message: str,
    source_platform: Optional[str] = None,
) -> Optional[OpportunityRoutingOutcome]:
    """Return a routed outcome only when the prompt clearly needs it."""

    if _active_profile_name(agent) == OPPORTUNITY_RADAR_PROFILE:
        return None

    if not should_route_opportunity_request(user_message):
        return None
    try:
        return route_opportunity_request(
            agent,
            user_message=user_message,
            original_user_message=original_user_message,
            source_platform=source_platform,
        )
    except OpportunityRoutingBusyError as exc:
        mode = classify_opportunity_mode(user_message)
        return OpportunityRoutingOutcome(
            routed=True,
            success=False,
            final_response=str(exc),
            mode=mode,
            turn_exit_reason="opportunity_route_busy",
            error=str(exc),
        )


__all__ = [
    "OpportunityRoutingBusyError",
    "OpportunityRoutingError",
    "OpportunityRoutingOutcome",
    "build_dispatch_payload",
    "classify_opportunity_mode",
    "extract_explicit_constraints",
    "maybe_route_opportunity_request",
    "route_opportunity_request",
    "should_route_opportunity_request",
]
