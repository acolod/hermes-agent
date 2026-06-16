"""Shared helpers for extracting visible user-facing text from structured payloads."""

from __future__ import annotations

import ast
import json
import re
from typing import Any, List

MAX_NORMALIZED_TEXT_LENGTH = 65_536
MAX_CONTENT_LIST_SIZE = 1_000
_VISIBLE_TEXT_TYPES = frozenset({"text", "input_text", "output_text", "summary_text"})
_LEAKAGE_FIELD_HINTS = frozenset({
    "events",
    "created_at",
    "run_id",
    "tenant",
    "assignee",
    "status",
    "worker_context",
    "workspace_path",
    "metadata",
    "summary",
})
_REPR_TEXT_PATTERN = re.compile(
    r"(?s)[A-Za-z_][A-Za-z0-9_]*\((?=[^)]*\btype=(['\"])(text|input_text|output_text|summary_text)\1)(?=[^)]*\btext=(['\"])(.*?)\3)[^)]*\)"
)


def _truncate_text(text: str) -> str:
    return text[:MAX_NORMALIZED_TEXT_LENGTH] if len(text) > MAX_NORMALIZED_TEXT_LENGTH else text


def _looks_like_leak_payload(parsed: Any, original_text: str = "") -> bool:
    if isinstance(parsed, dict):
        keys = {str(k) for k in parsed.keys()}
        return bool(keys & _LEAKAGE_FIELD_HINTS)
    if isinstance(parsed, list):
        if not parsed:
            return False
        if any(isinstance(item, dict) and _looks_like_leak_payload(item) for item in parsed[:20]):
            return True
    lowered = original_text.lower()
    return any(f'"{key}"' in lowered or f"'{key}'" in lowered for key in _LEAKAGE_FIELD_HINTS)


def _has_structured_type_markers(parsed: Any) -> bool:
    if isinstance(parsed, dict):
        if "type" in parsed:
            return True
        for key in ("content", "output"):
            if key in parsed and _has_structured_type_markers(parsed.get(key)):
                return True
        return False
    if isinstance(parsed, list):
        return any(_has_structured_type_markers(item) for item in parsed[:50])
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
        text = content.strip()
        if not text:
            return ""

        repr_text = _extract_repr_text(text)
        if repr_text:
            return repr_text

        if text[:1] in "[{":
            for parser in (json.loads, ast.literal_eval):
                try:
                    parsed = parser(text)
                except Exception:
                    continue
                normalized = normalize_visible_text(parsed, _max_depth=_max_depth, _depth=_depth + 1)
                if normalized:
                    return normalized
                if isinstance(parsed, (list, dict)) and (
                    _looks_like_leak_payload(parsed, text) or _has_structured_type_markers(parsed)
                ):
                    return ""
                if isinstance(parsed, (list, dict)):
                    break

        return _truncate_text(text)

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
