"""Shared helpers for extracting visible user-facing text from structured payloads."""

from __future__ import annotations

import ast
import json
import re
from typing import Any, List

MAX_NORMALIZED_TEXT_LENGTH = 65_536
MAX_CONTENT_LIST_SIZE = 1_000
_VISIBLE_TEXT_TYPES = frozenset({"text", "input_text", "output_text", "summary_text"})
_KNOWN_STRUCTURED_TYPES = _VISIBLE_TEXT_TYPES | frozenset({
    "message",
    "tool_use",
    "tool_result",
    "thinking",
    "reasoning",
    "image",
    "input_image",
    "output_image",
    "audio",
    "input_audio",
    "output_audio",
})
_LEAKAGE_ANCHOR_FIELDS = frozenset({
    "events",
    "run_id",
    "worker_context",
    "workspace_path",
    "tenant",
    "assignee",
})
_REPR_TEXT_PATTERN = re.compile(
    r"(?s)[A-Za-z_][A-Za-z0-9_]*\((?=[^)]*\btype=(['\"])(text|input_text|output_text|summary_text)\1)(?=[^)]*\btext=(['\"])(.*?)\3)[^)]*\)"
)


def _truncate_text(text: str) -> str:
    return text[:MAX_NORMALIZED_TEXT_LENGTH] if len(text) > MAX_NORMALIZED_TEXT_LENGTH else text


def _leak_anchor_fields(parsed: Any, *, _depth: int = 0) -> set[str]:
    if _depth > 4:
        return set()
    if isinstance(parsed, dict):
        found = {str(key) for key in parsed if str(key) in _LEAKAGE_ANCHOR_FIELDS}
        for value in list(parsed.values())[:20]:
            found.update(_leak_anchor_fields(value, _depth=_depth + 1))
        return found
    if isinstance(parsed, list):
        found: set[str] = set()
        for item in parsed[:20]:
            found.update(_leak_anchor_fields(item, _depth=_depth + 1))
        return found
    return set()


def _looks_like_leak_payload(parsed: Any) -> bool:
    """Recognize internal run payloads without treating ordinary JSON as leaks."""
    anchors = _leak_anchor_fields(parsed)
    return "events" in anchors and bool(
        anchors & {"run_id", "worker_context", "workspace_path", "tenant", "assignee"}
    )


def _has_recognized_typed_wrapper(parsed: Any) -> bool:
    if isinstance(parsed, dict):
        item_type = str(parsed.get("type") or "").strip().lower()
        if item_type in _KNOWN_STRUCTURED_TYPES:
            return True
        for key in ("content", "output"):
            if key in parsed and _has_recognized_typed_wrapper(parsed.get(key)):
                return True
        return False
    if isinstance(parsed, list):
        return any(_has_recognized_typed_wrapper(item) for item in parsed[:50])
    return False


def _extract_repr_text(text: str) -> str:
    parts = [match.group(4) for match in _REPR_TEXT_PATTERN.finditer(text) if match.group(4)]
    return _truncate_text("\n".join(parts)) if parts else ""


def normalize_visible_text(content: Any, *, _max_depth: int = 10, _depth: int = 0) -> str:
    """Return only the human-visible text from structured assistant content.

    This is intentionally conservative on user-facing boundaries: it extracts
    visible text from typed content blocks / reprs and suppresses known raw
    leak payloads when no visible text can be recovered.
    """
    if _depth > _max_depth:
        return ""
    if content is None:
        return ""

    if isinstance(content, str):
        original_text = content
        text = original_text.strip()
        if not text:
            return _truncate_text(original_text)

        repr_text = _extract_repr_text(text)
        if repr_text:
            return repr_text

        if text[:1] in "[{":
            for parser in (json.loads, ast.literal_eval):
                try:
                    parsed = parser(text)
                except Exception:
                    continue
                if _has_recognized_typed_wrapper(parsed):
                    return normalize_visible_text(
                        parsed,
                        _max_depth=_max_depth,
                        _depth=_depth + 1,
                    )
                if _looks_like_leak_payload(parsed):
                    return ""
                if isinstance(parsed, (list, dict)):
                    break

        return _truncate_text(original_text)

    if isinstance(content, list):
        parts: List[str] = []
        items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
        total_len = 0
        for item in items:
            nested = normalize_visible_text(item, _max_depth=_max_depth, _depth=_depth + 1)
            if not nested:
                continue
            parts.append(nested)
            total_len += len(nested)
            if total_len >= MAX_NORMALIZED_TEXT_LENGTH:
                break
        result = "\n".join(parts)
        return _truncate_text(result)

    if isinstance(content, dict):
        item_type = str(content.get("type") or "").strip().lower()
        if item_type in _VISIBLE_TEXT_TYPES:
            text = content.get("text", "")
            return _truncate_text(str(text)) if text else ""
        if item_type == "message" and "content" in content:
            return normalize_visible_text(content.get("content"), _max_depth=_max_depth, _depth=_depth + 1)
        if content.get("role") in {"assistant", "user", "system", "tool"} and "content" in content:
            return normalize_visible_text(content.get("content"), _max_depth=_max_depth, _depth=_depth + 1)
        if _looks_like_leak_payload(content):
            return ""
        return ""

    item_type = str(getattr(content, "type", "") or "").strip().lower()
    if item_type in _VISIBLE_TEXT_TYPES:
        text = getattr(content, "text", "")
        return _truncate_text(str(text)) if text else ""
    if item_type == "message" and hasattr(content, "content"):
        return normalize_visible_text(getattr(content, "content"), _max_depth=_max_depth, _depth=_depth + 1)
    if item_type in _KNOWN_STRUCTURED_TYPES:
        return ""
    if hasattr(content, "role") and hasattr(content, "content"):
        return normalize_visible_text(getattr(content, "content"), _max_depth=_max_depth, _depth=_depth + 1)

    try:
        result = str(content)
        repr_text = _extract_repr_text(result)
        if repr_text:
            return repr_text
        return _truncate_text(result)
    except Exception:
        return ""
