"""
Email platform adapter for the Hermes gateway.

Allows users to interact with Hermes by sending emails.
Uses IMAP to receive and SMTP to send messages.

Environment variables:
    EMAIL_IMAP_HOST     — IMAP server host (e.g., imap.gmail.com)
    EMAIL_IMAP_PORT     — IMAP server port (default: 993)
    EMAIL_SMTP_HOST     — SMTP server host (e.g., smtp.gmail.com)
    EMAIL_SMTP_PORT     — SMTP server port (default: 587)
    EMAIL_ADDRESS       — Email address for the agent
    EMAIL_PASSWORD      — Email password or app-specific password
    EMAIL_POLL_INTERVAL — Seconds between mailbox checks (default: 15)
    EMAIL_ALLOWED_USERS — Comma-separated list of allowed sender addresses
"""

import asyncio
import email as email_lib
import hashlib
import html
import imaplib
import json
import logging
import os
import re
import smtplib
import socket

# Profile-scoped secret reader for multiplexing support (PR #50094)
from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret
import ssl
import uuid
import urllib.error
import urllib.request
import base64
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.utils import formatdate
from email import encoders
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from gateway.config import Platform, PlatformConfig
from utils import is_truthy_value

logger = logging.getLogger(__name__)


def _extract_one_json_object(raw: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    decoder = json.JSONDecoder()
    candidates = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    if len(candidates) != 1:
        raise ValueError("invalid Sam structured output: expected exactly one JSON object")
    return candidates[0]


def _parse_sam_structured_output(raw: str) -> Dict[str, str]:
    """Extract and validate exactly one restricted Sam draft object."""
    parsed = _extract_one_json_object(raw)
    required = {
        "outcome",
        "informational_content",
        "reason",
        "proposed_action_verb",
        "proposed_action_object",
        "proposed_action_target",
        "required_authority",
    }
    if set(parsed) != required:
        raise ValueError("invalid Sam structured output: schema mismatch")
    if not all(isinstance(parsed[key], str) for key in parsed):
        raise ValueError("invalid Sam structured output: fields must be strings")
    outcome = parsed["outcome"].strip().upper()
    informational_content = parsed["informational_content"].strip()
    reason = parsed["reason"].strip()
    if outcome not in {
        "DIRECT_REPLY",
        "SEND_AND_REVIEW_ACTION",
        "REVIEW_REQUIRED",
        "REFUSE",
    }:
        raise ValueError("invalid Sam structured output: invalid outcome")
    if not reason or (
        outcome in {"DIRECT_REPLY", "SEND_AND_REVIEW_ACTION"} and not informational_content
    ):
        raise ValueError("invalid Sam structured output: required field is empty")
    verb = parsed["proposed_action_verb"].strip()
    obj = parsed["proposed_action_object"].strip()
    target = parsed["proposed_action_target"].strip()
    authority = parsed["required_authority"].strip()
    if outcome == "SEND_AND_REVIEW_ACTION" and not all((verb, obj, target, authority)):
        raise ValueError("invalid Sam structured output: action fields are empty")
    return {
        "outcome": outcome,
        "informational_content": informational_content,
        "reason": reason,
        "proposed_action_verb": verb,
        "proposed_action_object": obj,
        "proposed_action_target": target,
        "required_authority": authority,
    }


def _parse_sam_validation_output(raw: str) -> Dict[str, str]:
    """Extract and validate exactly one restricted Sam validation object."""
    parsed = _extract_one_json_object(raw)
    required = {"verdict", "validation_marker", "reason", "effect"}
    if set(parsed) != required:
        raise ValueError("invalid Sam validation output: schema mismatch")
    if not all(isinstance(parsed[key], str) for key in parsed):
        raise ValueError("invalid Sam validation output: fields must be strings")
    verdict = parsed["verdict"].strip().upper()
    marker = parsed["validation_marker"].strip()
    reason = parsed["reason"].strip()
    effect = parsed["effect"].strip().upper()
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("invalid Sam validation output: invalid verdict")
    if not reason:
        raise ValueError("invalid Sam validation output: required field is empty")
    if effect not in {
        "INFORMATIONAL",
        "PROTECTED_ACTION",
        "PRIVATE_DISCLOSURE",
        "REFUSAL",
    }:
        raise ValueError("invalid Sam validation output: invalid effect")
    if verdict == "PASS" and marker != "SAM_RESTRICTED_VALIDATED_V1":
        raise ValueError("invalid Sam validation output: missing validation marker")
    if verdict == "FAIL" and marker:
        raise ValueError("invalid Sam validation output: unexpected validation marker")
    return {
        "verdict": verdict,
        "validation_marker": marker,
        "reason": reason,
        "effect": effect,
    }


def _get_esecret(name: str, default: str = "") -> str:
    """Scope-aware ``EMAIL_*`` read with the default-profile startup fallback.

    Secondary profiles run under ``_profile_runtime_scope`` — the scope is
    authoritative and a scoped miss returns ``default`` (no cross-profile
    borrow). The DEFAULT profile's adapter constructs and sends *unscoped*
    under multiplexing, where a bare ``get_secret`` would raise
    ``UnscopedSecretError`` and crash its email path; there ``os.environ``
    is that profile's own value, so fall back to it. Same pattern as the
    Slack ``SLACK_APP_TOKEN`` read (#59739) and the WhatsApp
    ``_get_wsecret`` fix (5438e9c629).
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


# Backwards-compatible alias for the name used by the original #59076 hunks.
_get_secret = _get_esecret


def _esecret_int(name: str, default: int) -> int:
    """Scope-aware integer read (``env_int`` variant of ``_get_esecret``)."""
    raw = str(_get_esecret(name, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _esecret_bool(name: str, default: bool = False) -> bool:
    """Scope-aware boolean read (``env_bool`` variant of ``_get_esecret``)."""
    return is_truthy_value(_get_esecret(name, ""), default=default)

# Automated sender patterns — emails from these are silently ignored
_NOREPLY_PATTERNS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notifications@",
    "automated@", "auto-confirm", "auto-reply", "automailer",
)

# RFC headers that indicate bulk/automated mail
_AUTOMATED_HEADERS = {
    "Auto-Submitted": lambda v: v.lower() != "no",
    "Precedence": lambda v: v.lower() in {"bulk", "list", "junk"},
    "X-Auto-Response-Suppress": lambda v: bool(v),
    "List-Unsubscribe": lambda v: bool(v),
}

# Gmail-safe max length per email body
MAX_MESSAGE_LENGTH = 50_000

SMTP_CONNECT_TIMEOUT = 30


def _create_ipv4_connection(
    host: str,
    port: int,
    timeout: float,
    source_address: Any = None,
) -> socket.socket:
    """Create a TCP connection using only IPv4 addresses.

    This mirrors ``socket.create_connection`` but constrains DNS resolution to
    ``AF_INET``.  It avoids mutating process-global socket functions, which
    matters because email sends run in executor threads.
    """
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(
        host, port, socket.AF_INET, socket.SOCK_STREAM
    ):
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"No IPv4 address found for {host}:{port}")


class _IPv4SMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):  # type: ignore[override]
        return _create_ipv4_connection(
            host,
            port,
            timeout,
            source_address=self.source_address,
        )


class _IPv4SMTP_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):  # type: ignore[override]
        raw_sock = _create_ipv4_connection(
            host,
            port,
            timeout,
            source_address=self.source_address,
        )
        return self.context.wrap_socket(
            raw_sock,
            server_hostname=getattr(self, "_host", host),
        )

# Supported image extensions for inline detection
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_REASONING_PREFIX_RE = re.compile(
    r"^\s*(?:💭\s*)?(?:\*\*)?Reasoning:?(?:\*\*)?\s*\n+```.*?```\s*\n*",
    re.IGNORECASE | re.DOTALL,
)


def _strip_email_scratch(text: str) -> str:
    """Remove gateway-only scratch blocks that should never be emailed."""
    return _REASONING_PREFIX_RE.sub("", text or "").lstrip()


def _inline_markdown_to_html(text: str) -> str:
    """Escape text, then apply a small safe subset of Markdown inline styling."""
    escaped = html.escape(text, quote=True)
    escaped = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2">\1</a>',
        escaped,
    )
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def _markdown_to_email_html(text: str) -> str:
    """Render a conservative, Gmail-friendly HTML body from plain Markdown.

    This intentionally avoids external dependencies and allows only generated
    tags from a small subset of Markdown. Raw HTML in the model output is always
    escaped, which keeps email delivery from becoming an HTML/script injection
    path while still making headings, bullets, links, and emphasis readable.
    """
    lines = (text or "").splitlines()
    parts: List[str] = []
    in_ul = False
    in_ol = False

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            parts.append("</ul>")
            in_ul = False
        if in_ol:
            parts.append("</ol>")
            in_ol = False

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            close_lists()
            continue

        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if heading:
            close_lists()
            level = len(heading.group(1))
            parts.append(f"<h{level}>{_inline_markdown_to_html(heading.group(2).strip())}</h{level}>")
            continue

        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        if bullet:
            if in_ol:
                parts.append("</ol>")
                in_ol = False
            if not in_ul:
                parts.append("<ul>")
                in_ul = True
            parts.append(f"<li>{_inline_markdown_to_html(bullet.group(1).strip())}</li>")
            continue

        numbered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if numbered:
            if in_ul:
                parts.append("</ul>")
                in_ul = False
            if not in_ol:
                parts.append("<ol>")
                in_ol = True
            parts.append(f"<li>{_inline_markdown_to_html(numbered.group(1).strip())}</li>")
            continue

        close_lists()
        parts.append(f"<p>{_inline_markdown_to_html(line.strip())}</p>")

    close_lists()
    body = "\n".join(parts) or "<p></p>"
    return (
        "<!doctype html>\n"
        "<html><body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "line-height:1.55;color:#1f2937;background:#ffffff;margin:0;padding:0;\">\n"
        "<div style=\"max-width:760px;margin:0 auto;padding:24px;\">\n"
        f"{body}\n"
        "</div></body></html>"
    )


def _prepare_email_bodies(body: str) -> tuple[str, str]:
    """Return sanitized plain text plus safe HTML for email delivery."""
    plain = _strip_email_scratch(body or "")
    return plain, _markdown_to_email_html(plain)


def _attach_body_parts(msg: MIMEMultipart, body: str, *, html_enabled: bool) -> None:
    """Attach email body with a plain fallback and optional HTML alternative."""
    plain, html_body = _prepare_email_bodies(body)
    if html_enabled:
        alternative = MIMEMultipart("alternative")
        alternative.attach(MIMEText(plain, "plain", "utf-8"))
        alternative.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alternative)
    else:
        msg.attach(MIMEText(plain, "plain", "utf-8"))


def _send_imap_id(imap: "imaplib.IMAP4") -> None:
    """Send RFC 2971 IMAP ID command identifying this client.

    Required by 163/NetEase mailbox after LOGIN: without it, every UID
    SEARCH/FETCH returns ``BYE Unsafe Login`` and disconnects.  Other
    IMAP servers either honor it silently or reject the unknown command;
    we swallow failures so non-supporting servers keep working.
    """
    try:
        try:
            from hermes_cli import __version__ as _hermes_version
        except Exception:  # noqa: BLE001 — keep ID best-effort if import fails
            _hermes_version = "0"
        imap.xatom(
            "ID",
            f'("name" "hermes-agent" "version" "{_hermes_version}" '
            '"vendor" "NousResearch" '
            '"support-email" "noreply@nousresearch.com")',
        )
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal
        logger.debug("[Email] IMAP ID command not accepted: %s", e)


def _is_automated_sender(address: str, headers: dict) -> bool:
    """Return True if this email is from an automated/noreply source."""
    addr = address.lower()
    if any(pattern in addr for pattern in _NOREPLY_PATTERNS):
        return True
    for header, check in _AUTOMATED_HEADERS.items():
        value = headers.get(header, "")
        if value and check(value):
            return True
    return False
    
def check_email_requirements() -> bool:
    """Check if email platform settings are available and non-blank.

    Treats blank/whitespace-only values as missing so an abandoned setup that
    left empty ``EMAIL_*`` keys in ``.env`` does not enable the platform (#40715).
    """
    addr = _get_secret("EMAIL_ADDRESS", "").strip()
    pwd = _get_secret("EMAIL_PASSWORD", "").strip()
    imap = _get_secret("EMAIL_IMAP_HOST", "").strip()
    smtp = _get_secret("EMAIL_SMTP_HOST", "").strip()
    return all([addr, pwd, imap, smtp])


def _decode_header_value(raw: str) -> str:
    """Decode an RFC 2047 encoded email header into a plain string."""
    parts = decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _extract_text_body(msg: email_lib.message.Message) -> str:
    """Extract the plain-text body from a potentially multipart email."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            # Skip attachments
            if "attachment" in disposition:
                continue
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: try text/html and strip tags
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    return _strip_html(html)
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                return _strip_html(text)
            return text
        return ""


def _strip_html(html: str) -> str:
    """Naive HTML tag stripper for fallback text extraction."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_email_address(raw: str) -> str:
    """Extract bare email address from 'Name <addr>' format."""
    match = re.search(r"<([^>]+)>", raw)
    if match:
        return match.group(1).strip().lower()
    return raw.strip().lower()


def _domain_of(address: str) -> str:
    """Return the lowercased domain part of an email address, or ''."""
    _, _, domain = address.rpartition("@")
    return domain.strip().lower()


def _domains_aligned(a: str, b: str) -> bool:
    """Return True if two domains are equal or in an organizational
    parent/subdomain relationship (relaxed DMARC alignment).

    DMARC relaxed alignment treats ``mail.example.com`` as aligned with
    ``example.com``. We approximate organizational alignment by checking
    exact equality or that one domain is a dot-suffix of the other.
    """
    a = (a or "").strip().lower().rstrip(".")
    b = (b or "").strip().lower().rstrip(".")
    if not a or not b:
        return False
    if a == b:
        return True
    return a.endswith("." + b) or b.endswith("." + a)


# Match a single "method=result" token in an Authentication-Results header,
# e.g. ``dmarc=pass`` or ``spf=fail``.
_AUTH_METHOD_RE = re.compile(
    r"\b(dmarc|dkim|spf)\s*=\s*([a-z]+)", re.IGNORECASE
)
# Match a property value like ``header.from=example.com`` or
# ``smtp.mailfrom=user@example.com``.
_AUTH_PROP_RE = re.compile(
    r"\b(header\.from|header\.d|smtp\.mailfrom|smtp\.from|envelope-from)\s*=\s*([^\s;]+)",
    re.IGNORECASE,
)


def _verify_sender_authentication(
    msg: email_lib.message.Message,
    from_addr: str,
    *,
    authserv_id: str = "",
) -> Tuple[bool, str]:
    """Verify that the message's ``From:`` domain is authenticated.

    The ``From:`` header is attacker-controlled and is never authenticated by
    IMAP delivery, so an allowlist keyed on ``From:`` alone is trivially
    spoofable (GHSA-rxqh-5572-8m77). The only trustworthy signal is the
    ``Authentication-Results`` header that the *receiving* mail server (the one
    we IMAP into) stamps after running SPF/DKIM/DMARC. That header is prepended
    by our own server, so the topmost instance is the one we trust; any
    ``Authentication-Results`` an attacker injected into the body of their
    message sorts below it.

    Returns ``(authenticated, reason)``. ``authenticated`` is True when:
      * a DMARC pass is recorded for the From domain, OR
      * an SPF pass aligned with the From domain, OR
      * a DKIM pass aligned (``header.d``) with the From domain.

    When no ``Authentication-Results`` header is present at all, we return
    ``(False, "no Authentication-Results header")`` — fail-closed. Operators
    whose mail server does not stamp this header can opt out of the check
    (see ``EmailAdapter._require_authenticated_sender``).
    """
    from_domain = _domain_of(from_addr)
    if not from_domain:
        return False, "missing From domain"

    # get_all preserves header order; the receiving server prepends its result,
    # so the FIRST Authentication-Results is the trusted one. We pin to the
    # configured authserv-id when provided to defend against an injected header
    # that happens to sort first.
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        return False, "no Authentication-Results header"

    trusted = None
    for raw in headers:
        value = " ".join(str(raw).split())
        if authserv_id:
            # authserv-id is the first token before the first ';'
            serv = value.split(";", 1)[0].strip().lower()
            if not _domains_aligned(serv, authserv_id) and serv != authserv_id.lower():
                continue
        trusted = value
        break
    if trusted is None:
        return False, "no Authentication-Results from trusted authserv-id"

    methods = {m.lower(): r.lower() for m, r in _AUTH_METHOD_RE.findall(trusted)}
    props = {p.lower(): v.strip().strip('"') for p, v in _AUTH_PROP_RE.findall(trusted)}

    # 1) DMARC pass is the strongest signal — DMARC already enforces From
    #    alignment, so a pass means the From domain is authenticated.
    if methods.get("dmarc") == "pass":
        return True, "dmarc=pass"

    # 2) SPF pass aligned with the From domain (the envelope/MAIL FROM domain
    #    must match the From domain).
    if methods.get("spf") == "pass":
        spf_domain = _domain_of(props.get("smtp.mailfrom", "")) or props.get(
            "smtp.from", ""
        ) or props.get("envelope-from", "")
        spf_domain = _domain_of(spf_domain) if "@" in spf_domain else spf_domain
        if _domains_aligned(spf_domain, from_domain):
            return True, "spf=pass aligned"

    # 3) DKIM pass aligned with the From domain (the signing domain header.d
    #    must align with the From domain).
    if methods.get("dkim") == "pass":
        dkim_domain = props.get("header.d", "") or _domain_of(props.get("header.from", ""))
        if _domains_aligned(dkim_domain, from_domain):
            return True, "dkim=pass aligned"

    return False, f"authentication failed ({trusted[:120]})"


def _email_domain(raw: str) -> str:
    """Extract the domain part from an email or display-name address."""
    addr = _extract_email_address(raw)
    if "@" not in addr:
        raise ValueError(f"Invalid email address: {raw}")
    return addr.split("@", 1)[1]


def _extract_attachments(
    msg: email_lib.message.Message,
    skip_attachments: bool = False,
    metadata_only: bool = False,
) -> List[Dict[str, Any]]:
    """Extract attachment metadata and cache files locally.

    When *skip_attachments* is True, all attachment/inline parts are ignored
    (useful for malware protection or bandwidth savings).
    """
    attachments = []
    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        if skip_attachments and ("attachment" in disposition or "inline" in disposition):
            continue
        if "attachment" not in disposition and "inline" not in disposition:
            continue
        # Skip text/plain and text/html body parts
        content_type = part.get_content_type()
        if content_type in {"text/plain", "text/html"} and "attachment" not in disposition:
            continue

        filename = part.get_filename()
        if filename:
            filename = _decode_header_value(filename)
        else:
            ext = part.get_content_subtype() or "bin"
            filename = f"attachment.{ext}"

        if metadata_only:
            raw_payload = part.get_payload(decode=False)
            raw_size = len(raw_payload) if isinstance(raw_payload, (str, bytes)) else None
            attachments.append({
                "filename": filename,
                "type": "image" if Path(filename).suffix.lower() in _IMAGE_EXTS else "document",
                "media_type": content_type,
                "size": raw_size,
            })
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        ext = Path(filename).suffix.lower()
        if ext in _IMAGE_EXTS:
            try:
                cached_path = cache_image_from_bytes(payload, ext)
            except ValueError:
                logger.debug("Skipping non-image attachment %s (invalid magic bytes)", filename)
                continue
            attachments.append({
                "path": cached_path,
                "filename": filename,
                "type": "image",
                "media_type": content_type,
            })
        else:
            cached_path = cache_document_from_bytes(payload, filename)
            attachments.append({
                "path": cached_path,
                "filename": filename,
                "type": "document",
                "media_type": content_type,
            })

    return attachments


class EmailAdapter(BasePlatformAdapter):
    """Email gateway adapter using IMAP (receive) and SMTP (send)."""

    supports_async_delivery: bool = False
    supports_unsolicited_delivery: bool = False
    _ALLOWED_EMAIL_PURPOSES = {
        "direct_reply",
        "explicit_review_packet",
        "explicit_notification",
    }

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.EMAIL)

        # Resolve connection settings from the env vars first, then fall back to
        # PlatformConfig.extra (address/imap_host/smtp_host) — the canonical dict
        # gateway.config populates and that the "connected" check, the
        # send-helper, and `hermes config show` already read. Without the
        # fallback a config.yaml-only setup left these empty. Host/address values
        # are stripped: a stray space or newline made IMAP4_SSL raise the
        # misleading ``[Errno 8] nodename nor servname`` (an unresolvable name)
        # instead of an obvious "host not set" error.
        extra = config.extra or {}
        self._address = (_get_secret("EMAIL_ADDRESS", "") or extra.get("address", "")).strip()
        self._password = _get_secret("EMAIL_PASSWORD", "")
        self._imap_host = (_get_secret("EMAIL_IMAP_HOST", "") or extra.get("imap_host", "")).strip()
        self._imap_port = _esecret_int("EMAIL_IMAP_PORT", 993)
        self._smtp_host = (_get_secret("EMAIL_SMTP_HOST", "") or extra.get("smtp_host", "")).strip()
        self._smtp_port = _esecret_int("EMAIL_SMTP_PORT", 587)
        self._poll_interval = _esecret_int("EMAIL_POLL_INTERVAL", 15)

        # Extra behavior is configured via config.yaml under platforms.email.extra.
        self._login_address = (extra.get("login_address") or self._address).strip()
        self._from_address = (extra.get("from_address") or self._login_address).strip()
        self._reply_to_address = (extra.get("reply_to_address") or self._from_address).strip()
        self._resend_api_key_path = (extra.get("resend_api_key_path") or "").strip()
        self._email_format = str(extra.get("format", "html") or "html").strip().lower()
        if self._email_format not in {"html", "plain"}:
            self._email_format = "html"

        # Skip attachments — configured via config.yaml:
        #   platforms:
        #     email:
        #       skip_attachments: true
        self._skip_attachments = extra.get("skip_attachments", False)

        # Require the sender's From: domain to be authenticated (SPF/DKIM/DMARC)
        # before trusting it for authorization. The From: header is
        # attacker-controlled and unauthenticated by IMAP, so an allowlist keyed
        # on it alone is spoofable (GHSA-rxqh-5572-8m77). Default ON (fail-closed).
        #
        # Operators whose receiving mail server does not stamp an
        # Authentication-Results header can opt out via config.yaml:
        #   platforms:
        #     email:
        #       require_authenticated_sender: false
        # or the EMAIL_TRUST_FROM_HEADER=true env mirror (parity with the other
        # EMAIL_* access-control vars). When allow-all is in effect the operator
        # has already chosen to accept any sender, so the check is moot and the
        # gate below is skipped.
        if "require_authenticated_sender" in extra:
            self._require_authenticated_sender = bool(extra["require_authenticated_sender"])
        elif _esecret_bool("EMAIL_TRUST_FROM_HEADER", False):
            self._require_authenticated_sender = False
        else:
            self._require_authenticated_sender = True

        # Optional authserv-id to pin Authentication-Results to the operator's
        # own receiving server (defends against an injected header that sorts
        # first). Defaults to the From-domain of the agent's own address.
        self._authserv_id = (
            extra.get("authserv_id", "") or _get_secret("EMAIL_AUTHSERV_ID", "")
        ).strip().lower()

        # Track message IDs we've already processed to avoid duplicates
        self._seen_uids: set = set()
        self._seen_uids_max: int = 2000   # cap to prevent unbounded memory growth
        self._poll_task: Optional[asyncio.Task] = None

        # Request-scoped RFC thread metadata, keyed by the exact inbound
        # Message-ID. The sender cache remains for compatibility but is never an
        # implicit fallback for unrelated sends.
        self._thread_context: Dict[str, Dict[str, str]] = {}
        self._thread_context_by_message_id: Dict[str, Dict[str, str]] = {}

        # Authenticated Sam mail is handled by a dedicated no-session route.
        # It is configured explicitly and remains outside EMAIL_ALLOWED_USERS.
        self._sam_route = self._build_sam_restricted_route(extra)

        logger.info(
            "[Email] Adapter initialized (login=%s, from=%s, reply_to=%s, resend=%s)",
            self._login_address,
            self._from_address,
            self._reply_to_address,
            bool(self._resend_api_key_path),
        )

    def _build_sam_restricted_route(self, extra: Dict[str, Any]):
        if not bool(extra.get("sam_restricted_route_enabled", False)):
            return None
        from plugins.platforms.email.sam_restricted_route import SamRestrictedRoute

        state_path = Path(str(
            extra.get("sam_restricted_state_path")
            or (get_hermes_home() / "email-intake" / "sam-restricted-state.json")
        )).expanduser()
        return SamRestrictedRoute(
            state_path=state_path,
            shared_context_path=Path(str(
                extra.get("sam_shared_context_path")
                or (get_hermes_home() / "shared-context" / "sam-household.json")
            )).expanduser(),
            shared_context_root=get_hermes_home() / "shared-context",
            alex_email=str(
                extra.get("sam_restricted_alex_email") or "alexcolodner@gmail.com"
            ).strip(),
            send_email=self._send_sam_route_email,
            send_telegram=self._send_sam_route_telegram,
            draft=self._draft_sam_restricted,
        )

    async def _send_sam_route_email(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        purpose: str,
        reply_to_message_id: Optional[str],
        references: Optional[str],
        idempotency_key: str,
    ) -> str:
        if not self._resend_api_key_path:
            raise RuntimeError("restricted Sam delivery requires provider idempotency")
        delivery_key = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
        result = await self.send(
            to,
            body,
            reply_to=reply_to_message_id,
            metadata={
                "email_purpose": purpose,
                "email_subject": subject,
                "email_references": references,
                "email_idempotency_key": delivery_key,
                "email_message_id": f"<email-{delivery_key}@{_email_domain(self._from_address)}>",
            },
        )
        if not result.success:
            raise RuntimeError(result.error or "restricted email delivery failed")
        return str(result.message_id or "")

    async def _send_sam_route_telegram(self, text: str) -> str:
        chat_id = str(
            (self.config.extra or {}).get("sam_restricted_telegram_chat_id") or ""
        ).strip()
        if not chat_id:
            raise RuntimeError("sam_restricted_telegram_chat_id is not configured")
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        from tools.send_message_tool import _send_telegram

        result = await _send_telegram(token, chat_id, text)
        if isinstance(result, dict) and result.get("success") is False:
            raise RuntimeError(str(result.get("error") or "Telegram delivery failed"))
        return str(result)

    async def _draft_sam_restricted(self, packet: Dict[str, Any]) -> Dict[str, str]:
        """Run isolated no-tools/no-memory draft and semantic validation turns."""
        loop = asyncio.get_running_loop()

        def _run() -> Dict[str, str]:
            from run_agent import AIAgent
            from plugins.platforms.email.sam_restricted_instruction import (
                SAM_RESTRICTED_DRAFT_INSTRUCTION,
                SAM_RESTRICTED_VALIDATION_INSTRUCTION,
            )

            def _make_agent(system_prompt: str) -> Any:
                return AIAgent(
                    provider=str(
                        (self.config.extra or {}).get("sam_restricted_provider")
                        or "openai-codex"
                    ),
                    model=str(
                        (self.config.extra or {}).get("sam_restricted_model")
                        or "gpt-5.6-sol"
                    ),
                    fallback_model={
                        "provider": str(
                            (self.config.extra or {}).get("sam_restricted_fallback_provider")
                            or "modelrelay"
                        ),
                        "model": str(
                            (self.config.extra or {}).get("sam_restricted_fallback_model")
                            or "qwen3-32b"
                        ),
                    },
                    max_iterations=1,
                    enabled_toolsets=[],
                    skip_memory=True,
                    skip_context_files=True,
                    load_soul_identity=False,
                    ephemeral_system_prompt=system_prompt,
                    quiet_mode=True,
                    verbose_logging=False,
                    platform="email-restricted",
                )

            def _draft_once() -> Dict[str, str]:
                agent = _make_agent(SAM_RESTRICTED_DRAFT_INSTRUCTION)
                try:
                    prompt = json.dumps(packet, ensure_ascii=False)
                    result = agent.run_conversation(prompt, conversation_history=[])
                    raw = str((result or {}).get("final_response") or "").strip()
                    try:
                        return _parse_sam_structured_output(raw)
                    except ValueError:
                        kind = "html" if raw.lstrip().lower().startswith("<html") else "malformed"
                        logger.warning(
                            "[Email] Restricted Sam draft output invalid; kind=%s bytes=%d sha256=%s; attempting one repair",
                            kind,
                            len(raw.encode("utf-8")),
                            hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
                        )
                        repair_prompt = json.dumps(
                            {
                                "task": (
                                    "The previous output was invalid. Return exactly one corrected JSON object "
                                    "with only string fields outcome, informational_content, reason, "
                                    "proposed_action_verb, proposed_action_object, proposed_action_target, "
                                    "required_authority. outcome must be DIRECT_REPLY, "
                                    "SEND_AND_REVIEW_ACTION, REVIEW_REQUIRED, or REFUSE. DIRECT_REPLY and "
                                    "SEND_AND_REVIEW_ACTION require nonempty informational_content. "
                                    "SEND_AND_REVIEW_ACTION requires all four proposed action fields. "
                                    "Do not include markdown or prose."
                                ),
                                "packet": packet,
                                "invalid_output": raw[:4000],
                            },
                            ensure_ascii=False,
                        )
                        repaired = agent.run_conversation(repair_prompt, conversation_history=[])
                        repaired_raw = str((repaired or {}).get("final_response") or "").strip()
                        try:
                            return _parse_sam_structured_output(repaired_raw)
                        except ValueError as repair_error:
                            raise ValueError(
                                "invalid Sam structured output after one repair"
                            ) from repair_error
                finally:
                    try:
                        agent.close()
                    except Exception:
                        pass

            def _validate_once(draft_result: Dict[str, str]) -> Dict[str, str]:
                agent = _make_agent(SAM_RESTRICTED_VALIDATION_INSTRUCTION)
                try:
                    validation_prompt = json.dumps(
                        {
                            "new_sam_message": str(packet.get("body") or ""),
                            "draft_result": draft_result,
                        },
                        ensure_ascii=False,
                    )
                    result = agent.run_conversation(validation_prompt, conversation_history=[])
                    raw = str((result or {}).get("final_response") or "").strip()
                    return _parse_sam_validation_output(raw)
                finally:
                    try:
                        agent.close()
                    except Exception:
                        pass

            draft_result = _draft_once()
            validation = _validate_once(draft_result)
            allowed_effects = {
                "DIRECT_REPLY": {"INFORMATIONAL"},
                "SEND_AND_REVIEW_ACTION": {"PROTECTED_ACTION"},
                "REVIEW_REQUIRED": {"PROTECTED_ACTION", "PRIVATE_DISCLOSURE"},
                "REFUSE": {"REFUSAL"},
            }
            if (
                validation["verdict"] == "PASS"
                and validation["effect"] not in allowed_effects[draft_result["outcome"]]
            ):
                validation = {
                    "verdict": "FAIL",
                    "validation_marker": "",
                    "reason": "validator effect does not match drafted outcome",
                    "effect": validation["effect"],
                }
            merged = dict(draft_result)
            merged["validation_marker"] = validation["validation_marker"]
            merged["validation_reason"] = validation["reason"]
            merged["validation_effect"] = validation["effect"]
            return merged

        return await loop.run_in_executor(None, _run)

    def _build_reply_subject(
        self, to_addr: str, reply_to_msg_id: Optional[str] = None
    ) -> str:
        subject = "Hermes Agent"
        if reply_to_msg_id:
            ctx = self._thread_context_by_message_id.get(reply_to_msg_id, {})
            if not ctx:
                latest = self._thread_context.get(to_addr, {})
                if latest.get("message_id") == reply_to_msg_id:
                    ctx = latest
            subject = ctx.get("subject", subject)
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        return subject if reply_to_msg_id else subject.removeprefix("Re: ")

    def _build_thread_headers(self, to_addr: str, reply_to_msg_id: Optional[str] = None) -> tuple[str, dict[str, str]]:
        msg_id = f"<hermes-{uuid.uuid4().hex[:12]}@{_email_domain(self._from_address)}>"
        headers = {
            "Date": formatdate(localtime=True),
            "Message-ID": msg_id,
        }
        if reply_to_msg_id:
            headers["In-Reply-To"] = reply_to_msg_id
            ctx = self._thread_context_by_message_id.get(reply_to_msg_id, {})
            references = str(ctx.get("references") or "").split()
            if reply_to_msg_id not in references:
                references.append(reply_to_msg_id)
            headers["References"] = " ".join(references)
        return msg_id, headers

    def _read_resend_api_key(self) -> str:
        if not self._resend_api_key_path:
            raise RuntimeError("Resend is not configured for the email adapter")
        return Path(self._resend_api_key_path).read_text(encoding="utf-8").strip()

    def _send_via_resend(
        self,
        *,
        to_addr: str,
        subject: str,
        body: str,
        headers: Dict[str, str],
        attachments: Optional[List[Tuple[str, bytes]]] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        msg_id = headers.get("Message-ID") or f"<hermes-{uuid.uuid4().hex[:12]}@{self._from_address.split('@')[1]}>"
        text_body, html_body = _prepare_email_bodies(body)
        payload: Dict[str, Any] = {
            "from": self._from_address,
            "to": [to_addr],
            "subject": subject,
            "text": text_body,
            "reply_to": self._reply_to_address,
            "headers": headers,
        }
        if self._email_format == "html":
            payload["html"] = html_body
        if attachments:
            payload["attachments"] = [
                {
                    "filename": filename,
                    "content": base64.b64encode(content).decode("ascii"),
                }
                for filename, content in attachments
            ]
        request_headers = {
            "Authorization": f"Bearer {self._read_resend_api_key()}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes-EmailAdapter/1.0",
        }
        if idempotency_key:
            request_headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps(payload).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                response = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Resend HTTP {exc.code}: {body_text}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Resend connection failed: {exc}") from exc

        resend_id = response.get("id")
        logger.info("[Email] Sent via Resend to %s (subject: %s, resend_id=%s)", to_addr, subject, resend_id)
        return msg_id

    def _trim_seen_uids(self) -> None:
        """Keep only the most recent UIDs to prevent unbounded memory growth.

        IMAP UIDs are monotonically increasing integers. When the set grows
        beyond the cap, we keep only the highest half — old UIDs are safe to
        drop because new messages always have higher UIDs and IMAP's UNSEEN
        flag prevents re-delivery regardless.
        """
        if len(self._seen_uids) <= self._seen_uids_max:
            return
        try:
            # UIDs are bytes like b'1234' — sort numerically and keep top half
            sorted_uids = sorted(self._seen_uids, key=lambda u: int(u))
            keep = self._seen_uids_max // 2
            self._seen_uids = set(sorted_uids[-keep:])
            logger.debug("[Email] Trimmed seen UIDs to %d entries", len(self._seen_uids))
        except (ValueError, TypeError):
            # Fallback: just clear old entries if sort fails
            self._seen_uids = set(list(self._seen_uids)[-self._seen_uids_max // 2:])

    def _connect_smtp(self) -> smtplib.SMTP:
        """Create an SMTP connection, selecting the correct protocol for the port.

        Port 465 uses implicit TLS (``SMTP_SSL``).  All other ports use
        ``SMTP`` + ``STARTTLS``.

        When the host resolves to an IPv6 address that is unreachable
        (common on networks without IPv6 routing), the default connection can
        hang until the socket timeout expires.  We retry connection-level
        failures through an IPv4-only socket path, without mutating global
        resolver state.  TLS verification errors are not retried.

        Returns a connected SMTP object with TLS established — callers
        can proceed directly to ``login()``.
        """
        ctx = ssl.create_default_context()
        host = self._smtp_host
        port = self._smtp_port

        def _connect(*, ipv4_only: bool = False) -> smtplib.SMTP:
            """Attempt one SMTP connection."""
            smtp_cls = _IPv4SMTP if ipv4_only else smtplib.SMTP
            smtp_ssl_cls = _IPv4SMTP_SSL if ipv4_only else smtplib.SMTP_SSL
            if port == 465:
                return smtp_ssl_cls(host, port, timeout=SMTP_CONNECT_TIMEOUT, context=ctx)
            smtp = smtp_cls(host, port, timeout=SMTP_CONNECT_TIMEOUT)
            try:
                smtp.starttls(context=ctx)
            except Exception:
                smtp.close()
                raise
            return smtp

        try:
            return _connect()
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            if isinstance(exc, ssl.SSLError):
                raise
            # Connection-level failure (may be unreachable IPv6).
            # Retry with IPv4 only.
            return _connect(ipv4_only=True)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to the IMAP server and start polling for new messages."""
        # Validate up front so a missing host surfaces as an actionable config
        # error instead of IMAP4_SSL("") raising the cryptic
        # ``[Errno 8] nodename nor servname provided, or not known``.
        missing = [
            name
            for name, value in (
                ("EMAIL_ADDRESS", self._address),
                ("EMAIL_PASSWORD", self._password),
                ("EMAIL_IMAP_HOST", self._imap_host),
                ("EMAIL_SMTP_HOST", self._smtp_host),
            )
            if not value
        ]
        if missing:
            message = (
                "Not configured — missing "
                + ", ".join(missing)
                + ". Set it via `hermes gateway setup` (env) or platforms.email "
                "in config.yaml."
            )
            logger.error("[Email] %s", message)
            # Mark non-retryable so the gateway does NOT keep reconnecting against
            # an empty host. A blank-but-present env var (e.g. ``EMAIL_IMAP_HOST=``)
            # used to slip past the startup gate and drive an indefinite retry
            # loop that leaked memory until the host OOM-killed (#40715).
            self._set_fatal_error(
                "email_missing_configuration", message, retryable=False
            )
            return False

        try:
            # Test IMAP connection
            imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port, timeout=30)
            imap.login(self._login_address, self._password)
            _send_imap_id(imap)
            # Mark all existing messages as seen so we only process new ones
            imap.select("INBOX")
            status, data = imap.uid("search", None, "ALL")
            if status == "OK" and data and data[0]:
                for uid in data[0].split():
                    self._seen_uids.add(uid)
            # Keep only the most recent UIDs to prevent unbounded growth
            self._trim_seen_uids()
            imap.logout()
            logger.info("[Email] IMAP connection test passed. %d existing messages skipped.", len(self._seen_uids))
        except Exception as e:
            logger.error("[Email] IMAP connection failed: %s", e)
            return False

        try:
            # Test SMTP connection
            smtp = self._connect_smtp()
            try:
                smtp.login(self._login_address, self._password)
            finally:
                smtp.quit()
            logger.info("[Email] SMTP connection test passed.")
        except Exception as e:
            logger.error("[Email] SMTP connection failed: %s", e)
            return False

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        print(f"[Email] Connected as {self._login_address} (public from: {self._from_address})")
        return True

    async def disconnect(self) -> None:
        """Stop polling and disconnect."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("[Email] Disconnected.")

    async def _poll_loop(self) -> None:
        """Poll IMAP for new messages at regular intervals."""
        while self._running:
            try:
                await self._check_inbox()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[Email] Poll error: %s", e)
            await asyncio.sleep(self._poll_interval)

    async def _check_inbox(self) -> None:
        """Check INBOX for unseen messages and dispatch them."""
        # Run IMAP operations in a thread to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        messages = await loop.run_in_executor(None, self._fetch_new_messages)
        for msg_data in messages:
            await self._dispatch_message(msg_data)

    def _fetch_new_messages(self) -> List[Dict[str, Any]]:
        """Fetch new (unseen) messages from IMAP. Runs in executor thread."""
        results = []
        try:
            imap = imaplib.IMAP4_SSL(self._imap_host, self._imap_port, timeout=30)
            try:
                imap.login(self._login_address, self._password)
                _send_imap_id(imap)
                imap.select("INBOX")

                status, data = imap.uid("search", None, "UNSEEN")
                if status != "OK" or not data or not data[0]:
                    return results

                for uid in data[0].split():
                    if uid in self._seen_uids:
                        continue
                    self._seen_uids.add(uid)
                    # Trim periodically to prevent unbounded memory growth
                    if len(self._seen_uids) > self._seen_uids_max:
                        self._trim_seen_uids()

                    status, msg_data = imap.uid("fetch", uid, "(RFC822)")
                    if status != "OK":
                        continue

                    # IMAP fetch can return unexpected structures (e.g. a
                    # single bytes item instead of a list of tuples). Guard
                    # against IndexError / TypeError so one malformed response
                    # doesn't abort the batch — the UID is already in
                    # _seen_uids, so an abort would permanently skip the
                    # remaining messages in this batch.
                    try:
                        raw_email = msg_data[0][1]
                    except (IndexError, TypeError):
                        logger.warning(
                            "[Email] Unexpected IMAP response structure for UID %s, skipping",
                            uid,
                        )
                        continue
                    if not isinstance(raw_email, (bytes, bytearray)):
                        logger.warning(
                            "[Email] Non-bytes IMAP payload for UID %s, skipping", uid
                        )
                        continue
                    msg = email_lib.message_from_bytes(raw_email)

                    sender_raw = msg.get("From", "")
                    sender_addr = _extract_email_address(sender_raw)
                    sender_name = _decode_header_value(sender_raw)
                    # Remove email from name if present
                    if "<" in sender_name:
                        sender_name = sender_name.split("<")[0].strip().strip('"')

                    subject = _decode_header_value(msg.get("Subject", "(no subject)"))
                    message_id = msg.get("Message-ID", "")
                    in_reply_to = msg.get("In-Reply-To", "")
                    references = msg.get("References", "")
                    # Skip automated/noreply senders before any processing
                    msg_headers = dict(msg.items())
                    if _is_automated_sender(sender_addr, msg_headers):
                        logger.debug("[Email] Skipping automated sender: %s", sender_addr)
                        continue

                    # Verify the From: domain is authenticated (SPF/DKIM/DMARC)
                    # while the raw message — and its trusted
                    # Authentication-Results header — is still in scope. The
                    # verdict is consumed at dispatch where authorization is
                    # decided. From: is attacker-controlled, so this is the only
                    # place a spoof can be caught (GHSA-rxqh-5572-8m77).
                    sender_authenticated, auth_reason = _verify_sender_authentication(
                        msg, sender_addr, authserv_id=self._authserv_id
                    )

                    body = _extract_text_body(msg)
                    attachments = _extract_attachments(
                        msg,
                        skip_attachments=self._skip_attachments,
                        metadata_only=sender_addr.strip().lower() == "sammyphillips19@gmail.com",
                    )

                    results.append({
                        "uid": uid,
                        "sender_addr": sender_addr,
                        "sender_name": sender_name,
                        "subject": subject,
                        "message_id": message_id,
                        "in_reply_to": in_reply_to,
                        "references": references,
                        "body": body,
                        "attachments": attachments,
                        "date": msg.get("Date", ""),
                        "sender_authenticated": sender_authenticated,
                        "auth_reason": auth_reason,
                    })
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
        except Exception as e:
            logger.error("[Email] IMAP fetch error: %s", e)
        return results

    @staticmethod
    def _allow_all_senders() -> bool:
        """Return True when the operator opted into accepting any sender.

        Mirrors the gateway authz allow-all resolution: the per-platform
        EMAIL_ALLOW_ALL_USERS flag or the global GATEWAY_ALLOW_ALL_USERS flag.
        When either is set, sender identity is moot, so the From: authentication
        gate is skipped.
        """
        truthy = {"true", "1", "yes"}
        return (
            _get_secret("EMAIL_ALLOW_ALL_USERS", "").strip().lower() in truthy
            or os.getenv("GATEWAY_ALLOW_ALL_USERS", "").strip().lower() in truthy
        )

    @staticmethod
    def _allowlist_in_effect() -> bool:
        """Return True when a sender allowlist gates email access.

        Authorization keys on the From: address only when an allowlist is
        configured — the per-platform EMAIL_ALLOWED_USERS or the global
        GATEWAY_ALLOWED_USERS. When neither is set the gateway default-denies
        every sender regardless, so the spoofable From: identity grants nothing
        and the authentication gate is unnecessary.
        """
        return bool(
            _get_secret("EMAIL_ALLOWED_USERS", "").strip()
            or os.getenv("GATEWAY_ALLOWED_USERS", "").strip()
        )

    async def _maybe_handle_sam_receipt_command(self, msg_data: Dict[str, Any]) -> bool:
        sender = str(msg_data.get("sender_addr") or "").strip().lower()
        alex_email = str(
            (self.config.extra or {}).get("sam_restricted_alex_email")
            or "alexcolodner@gmail.com"
        ).strip().lower()
        if sender != alex_email or not bool(msg_data.get("sender_authenticated")):
            return False
        body = str(msg_data.get("body") or "").strip()
        match = re.match(
            r"^SAM\s+(APPROVE|DECLINE|EDIT)\s+(sam-[a-f0-9]{3,64})(?:\s*\n([\s\S]+))?$",
            body,
            re.IGNORECASE,
        )
        if not match:
            return False
        action, receipt_id, edited = match.groups()
        route = self._sam_route
        if action.upper() == "APPROVE":
            result = await route.approve(receipt_id)
        elif action.upper() == "DECLINE":
            result = await route.decline(receipt_id)
        else:
            result = await route.edit(receipt_id, edited or "")
        await self.send(
            sender,
            f"Sam receipt {receipt_id}: {result.get('status', 'unknown')}",
            metadata={
                "email_purpose": "explicit_notification",
                "email_subject": f"Sam receipt {receipt_id}",
            },
        )
        return True

    async def _dispatch_message(self, msg_data: Dict[str, Any]) -> None:
        """Convert a fetched email into a MessageEvent and dispatch it."""
        sender_addr = msg_data["sender_addr"]

        # The restricted Sam route is intentionally consumed before the normal
        # allowlist/session path. It never creates a reusable gateway session.
        sam_route = getattr(self, "_sam_route", None)
        if sam_route is not None and sender_addr.strip().lower() == "sammyphillips19@gmail.com":
            if await sam_route.handle(msg_data):
                return
        if sam_route is not None and await self._maybe_handle_sam_receipt_command(msg_data):
            return

        # Skip self-messages from either the login mailbox or the public alias.
        sender_addr_lc = sender_addr.lower()
        if sender_addr_lc in {self._login_address.lower(), self._from_address.lower()}:
            return

        # Never reply to automated senders
        if _is_automated_sender(sender_addr, {}):
            logger.debug("[Email] Dropping automated sender at dispatch: %s", sender_addr)
            return

        # Skip senders not in EMAIL_ALLOWED_USERS — prevents the adapter
        # from creating a MessageEvent (and thus thread context) for senders
        # that the gateway will never authorize.  Without this early guard,
        # a race between dispatch and authorization can result in the adapter
        # sending a reply even though the handler returned None.
        allowed_raw = _get_secret("EMAIL_ALLOWED_USERS", "").strip()
        if not allowed_raw:
            if _get_secret("EMAIL_ALLOW_ALL_USERS", "").strip().lower() not in {"true", "1", "yes"} and (
                os.getenv("GATEWAY_ALLOW_ALL_USERS", "").strip().lower() not in {"true", "1", "yes"}
            ):
                logger.debug(
                    "[Email] Dropping sender at dispatch — EMAIL_ALLOWED_USERS is unset "
                    "and open access is not opted in: %s",
                    sender_addr,
                )
                return
        else:
            allowed = {addr.strip().lower() for addr in allowed_raw.split(",") if addr.strip()}
            if sender_addr.lower() not in allowed:
                logger.debug("[Email] Dropping non-allowlisted sender at dispatch: %s", sender_addr)
                return

        # Reject spoofed senders. The allowlist (and the gateway's own authz)
        # key on sender_addr, which comes straight from the attacker-controlled
        # From: header — so an attacker can forge From: an-allowlisted@addr to
        # get authorized (GHSA-rxqh-5572-8m77). This only matters when an
        # allowlist is actually being used to GRANT access: if no allowlist is
        # configured the gateway default-denies everyone anyway, and if allow-all
        # is on the operator already accepts any sender. So enforce From:
        # authentication exactly when an allowlist is in effect and allow-all is
        # off. Fail-closed: an unauthenticated From: is dropped before it can be
        # matched against the allowlist.
        if (
            self._require_authenticated_sender
            and self._allowlist_in_effect()
            and not self._allow_all_senders()
            and not msg_data.get("sender_authenticated", False)
        ):
            logger.warning(
                "[Email] Dropping sender with unauthenticated From: %s (%s). "
                "If your mail server does not stamp Authentication-Results, set "
                "platforms.email.require_authenticated_sender: false (or "
                "EMAIL_TRUST_FROM_HEADER=true) to accept the risk.",
                sender_addr,
                msg_data.get("auth_reason", "no verdict"),
            )
            return

        subject = msg_data["subject"]
        body = msg_data["body"].strip()
        attachments = msg_data["attachments"]

        # Build message text: include subject as context
        text = body
        if subject and not subject.startswith("Re:"):
            text = f"[Subject: {subject}]\n\n{body}"

        # Determine message type and media
        media_urls = []
        media_types = []
        msg_type = MessageType.TEXT

        for att in attachments:
            media_urls.append(att["path"])
            media_types.append(att["media_type"])
            if att["type"] == "image" and msg_type == MessageType.TEXT:
                msg_type = MessageType.PHOTO
            elif att["type"] == "document":
                # Document wins over PHOTO for mixed attachments: run.py's
                # image handling keys off the per-path image/* mime type
                # regardless of message_type, but document-context injection
                # gates strictly on MessageType.DOCUMENT — so DOCUMENT is the
                # only classification that surfaces both.
                msg_type = MessageType.DOCUMENT

        # Store request-scoped thread context under the exact RFC Message-ID.
        inbound_message_id = str(msg_data.get("message_id") or "").strip()
        self._thread_context[sender_addr] = {
            "subject": subject,
            "message_id": inbound_message_id,
        }
        if inbound_message_id:
            self._thread_context_by_message_id[inbound_message_id] = {
                "subject": subject,
                "references": str(msg_data.get("references") or ""),
            }
            if len(self._thread_context_by_message_id) > self._seen_uids_max:
                self._thread_context_by_message_id.pop(
                    next(iter(self._thread_context_by_message_id)), None
                )

        source = self.build_source(
            chat_id=sender_addr,
            chat_name=msg_data["sender_name"] or sender_addr,
            chat_type="dm",
            user_id=sender_addr,
            user_name=msg_data["sender_name"] or sender_addr,
        )

        event = MessageEvent(
            text=text or "(empty email)",
            message_type=msg_type,
            source=source,
            message_id=msg_data["message_id"],
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=msg_data["in_reply_to"] or None,
        )

        logger.info("[Email] New message from %s: %s", sender_addr, subject)
        await self.handle_message(event)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send one explicitly purposed request-scoped email."""
        metadata = metadata or {}
        purpose = str(metadata.get("email_purpose") or "").strip().lower()
        if purpose not in self._ALLOWED_EMAIL_PURPOSES:
            return SendResult(
                success=False,
                error="Email send rejected: explicit email_purpose is required",
            )
        if purpose == "direct_reply" and not reply_to:
            return SendResult(
                success=False,
                error="Email direct_reply requires the triggering RFC Message-ID",
            )
        subject_override = metadata.get("email_subject")
        references_override = metadata.get("email_references")
        idempotency_key = str(metadata.get("email_idempotency_key") or "").strip() or None
        message_id_override = str(metadata.get("email_message_id") or "").strip() or None
        try:
            loop = asyncio.get_running_loop()
            message_id = await loop.run_in_executor(
                None,
                self._send_email,
                chat_id,
                content,
                reply_to,
                subject_override,
                purpose,
                references_override,
                idempotency_key,
                message_id_override,
            )
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error("[Email] Send failed to %s: %s", chat_id, e)
            return SendResult(success=False, error=str(e))

    def _message_id_domain(self) -> str:
        """Domain part for generated Message-IDs.

        EMAIL_ADDRESS may lack an ``@`` (misconfiguration); fall back to
        ``localhost`` instead of crashing send with an IndexError.
        """
        if "@" in self._address:
            return self._address.rsplit("@", 1)[-1] or "localhost"
        return "localhost"

    def _send_email(
        self,
        to_addr: str,
        body: str,
        reply_to_msg_id: Optional[str] = None,
        subject_override: Optional[str] = None,
        purpose: str = "direct_reply",
        references_override: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        message_id_override: Optional[str] = None,
    ) -> str:
        """Send an email reply. Runs in executor thread."""
        subject = subject_override or self._build_reply_subject(to_addr, reply_to_msg_id)
        msg_id, headers = self._build_thread_headers(to_addr, reply_to_msg_id)
        if message_id_override:
            msg_id = message_id_override
            headers["Message-ID"] = message_id_override
        if reply_to_msg_id and references_override:
            headers["References"] = str(references_override)

        if self._resend_api_key_path:
            return self._send_via_resend(
                to_addr=to_addr,
                subject=subject,
                body=body,
                headers=headers,
                idempotency_key=idempotency_key,
            )

        msg = MIMEMultipart()
        msg["From"] = self._from_address
        msg["To"] = to_addr
        msg["Subject"] = subject
        for header_name, header_value in headers.items():
            msg[header_name] = header_value
        if self._reply_to_address:
            msg["Reply-To"] = self._reply_to_address

        _attach_body_parts(msg, body, html_enabled=self._email_format == "html")

        smtp = self._connect_smtp()
        try:
            smtp.login(self._login_address, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()

        logger.info("[Email] Sent reply to %s (subject: %s)", to_addr, subject)
        return msg_id

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Email has no typing indicator — no-op."""

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image URL as part of an email body.

        Preserve the explicit email purpose and request-scoped metadata when
        delegating to ``send``; dropping it would make the fail-closed send
        contract reject every image delivery.
        """
        text = caption or ""
        text += f"\n\nImage: {image_url}"
        return await self.send(chat_id, text.strip(), reply_to, metadata)

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Send a batch of images as a single email with multiple MIME attachments.

        Local files are attached directly. URL images have their URL
        appended to the body (email adapter does not download remote
        images). No hard cap — email clients handle dozens of
        attachments fine, subject to SMTP message size limits.
        """
        if not images:
            return

        from urllib.parse import unquote as _unquote

        body_parts: List[str] = []
        local_paths: List[str] = []
        for image_url, alt_text in images:
            if alt_text:
                body_parts.append(alt_text)
            if image_url.startswith("file://"):
                local_path = _unquote(image_url[7:])
                if Path(local_path).exists():
                    local_paths.append(local_path)
                else:
                    logger.warning("[Email] Skipping missing image: %s", local_path)
            else:
                # Remote URLs just get linked in the body (parity with send_image)
                body_parts.append(f"Image: {image_url}")

        if not local_paths and not body_parts:
            return

        body = "\n\n".join(body_parts)

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self._send_email_with_attachments,
                chat_id,
                body,
                local_paths,
            )
        except Exception as e:
            logger.error("[Email] Multi-image send failed, falling back: %s", e, exc_info=True)
            await super().send_multiple_images(chat_id, images, metadata, human_delay)

    def _send_email_with_attachments(
        self,
        to_addr: str,
        body: str,
        file_paths: List[str],
    ) -> str:
        """Send an email with multiple file attachments."""
        subject = self._build_reply_subject(to_addr)
        msg_id, headers = self._build_thread_headers(to_addr)

        if self._resend_api_key_path:
            attachments: List[Tuple[str, bytes]] = []
            for file_path in file_paths:
                p = Path(file_path)
                try:
                    attachments.append((p.name, p.read_bytes()))
                except Exception as e:
                    logger.warning("[Email] Failed to attach %s for Resend: %s", file_path, e)
            return self._send_via_resend(
                to_addr=to_addr,
                subject=subject,
                body=body,
                headers=headers,
                attachments=attachments,
            )

        msg = MIMEMultipart()
        msg["From"] = self._from_address
        msg["To"] = to_addr
        msg["Subject"] = subject
        for header_name, header_value in headers.items():
            msg[header_name] = header_value
        if self._reply_to_address:
            msg["Reply-To"] = self._reply_to_address

        if body:
            _attach_body_parts(msg, body, html_enabled=self._email_format == "html")

        for file_path in file_paths:
            p = Path(file_path)
            try:
                with open(p, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header("Content-Disposition", f"attachment; filename={p.name}")
                    msg.attach(part)
            except Exception as e:
                logger.warning("[Email] Failed to attach %s: %s", file_path, e)

        smtp = self._connect_smtp()
        try:
            smtp.login(self._login_address, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()

        logger.info("[Email] Sent multi-attachment email to %s (%d files)", to_addr, len(file_paths))
        return msg_id

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a file as an email attachment."""
        try:
            loop = asyncio.get_running_loop()
            message_id = await loop.run_in_executor(
                None,
                self._send_email_with_attachment,
                chat_id,
                caption or "",
                file_path,
                file_name,
            )
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.error("[Email] Send document failed: %s", e)
            return SendResult(success=False, error=str(e))

    def _send_email_with_attachment(
        self,
        to_addr: str,
        body: str,
        file_path: str,
        file_name: Optional[str] = None,
    ) -> str:
        """Send an email with a file attachment."""
        subject = self._build_reply_subject(to_addr)
        msg_id, headers = self._build_thread_headers(to_addr)
        p = Path(file_path)
        fname = file_name or p.name

        if self._resend_api_key_path:
            return self._send_via_resend(
                to_addr=to_addr,
                subject=subject,
                body=body,
                headers=headers,
                attachments=[(fname, p.read_bytes())],
            )

        msg = MIMEMultipart()
        msg["From"] = self._from_address
        msg["To"] = to_addr
        msg["Subject"] = subject
        for header_name, header_value in headers.items():
            msg[header_name] = header_value
        if self._reply_to_address:
            msg["Reply-To"] = self._reply_to_address

        if body:
            _attach_body_parts(msg, body, html_enabled=self._email_format == "html")

        # Attach file
        with open(p, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={fname}")
            msg.attach(part)

        smtp = self._connect_smtp()
        try:
            smtp.login(self._login_address, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                smtp.close()

        return msg_id

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the email chat."""
        ctx = self._thread_context.get(chat_id, {})
        return {
            "name": chat_id,
            "type": "dm",
            "chat_id": chat_id,
            "subject": ctx.get("subject", ""),
        }


# ──────────────────────────────────────────────────────────────────────────
# Plugin migration glue (#41112 / #3823)
#
# Added when the Email adapter moved from gateway/platforms/email.py into this
# bundled plugin. register() exposes the platform via the registry, replacing
# the Platform.EMAIL elif in gateway/run.py, the _PLATFORM_CONNECTED_CHECKERS
# entry in gateway/config.py, the _PLATFORMS["email"] static dict in
# hermes_cli/gateway.py, and the _send_email dispatch in
# tools/send_message_tool.py. EMAIL_* env→PlatformConfig seeding stays in core.
# ──────────────────────────────────────────────────────────────────────────


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Out-of-process Email delivery via SMTP (one-shot). Implements the
    standalone_sender_fn contract; replaces the legacy _send_email helper."""
    import smtplib
    import ssl as _ssl
    from email.utils import formatdate

    extra = getattr(pconfig, "extra", {}) or {}
    address = extra.get("address") or _get_secret("EMAIL_ADDRESS", "")
    password = _get_secret("EMAIL_PASSWORD", "")
    smtp_host = extra.get("smtp_host") or _get_secret("EMAIL_SMTP_HOST", "")
    try:
        smtp_port = int(_get_secret("EMAIL_SMTP_PORT", "587") or "587")
    except (ValueError, TypeError):
        smtp_port = 587

    if not all([address, password, smtp_host]):
        return {"error": "Email not configured (EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_SMTP_HOST required)"}

    try:
        msg = MIMEMultipart()
        msg["From"] = address
        msg["To"] = chat_id
        msg["Subject"] = "Hermes Agent"
        msg["Date"] = formatdate(localtime=True)
        email_format = str(extra.get("format", "html") or "html").strip().lower()
        _attach_body_parts(msg, message, html_enabled=email_format != "plain")

        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls(context=_ssl.create_default_context())
        server.login(address, password)
        server.send_message(msg)
        server.quit()
        return {"success": True, "platform": "email", "chat_id": chat_id}
    except Exception as e:
        try:
            from tools.send_message_tool import _error as _e
            return _e(f"Email send failed: {e}")
        except Exception:
            return {"error": f"Email send failed: {e}"}


def _is_connected(config) -> bool:
    """Email is connected when an address is configured (in PlatformConfig.extra
    or via EMAIL_ADDRESS). Mirrors the legacy
    _PLATFORM_CONNECTED_CHECKERS[Platform.EMAIL] = bool(extra.get('address'))."""
    extra = getattr(config, "extra", {}) or {}
    if extra.get("address"):
        return True
    import hermes_cli.gateway as gateway_mod
    return bool((gateway_mod.get_env_value("EMAIL_ADDRESS") or "").strip())


def _build_adapter(config):
    """Factory wrapper that constructs EmailAdapter from a PlatformConfig."""
    return EmailAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="email",
        label="Email",
        adapter_factory=_build_adapter,
        check_fn=check_email_requirements,
        is_connected=_is_connected,
        required_env=["EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_SMTP_HOST"],
        install_hint="Email uses the Python stdlib (smtplib/imaplib) — no extra deps",
        allowed_users_env="EMAIL_ALLOWED_USERS",
        allow_all_env="EMAIL_ALLOW_ALL_USERS",
        cron_deliver_env_var="EMAIL_HOME_ADDRESS",
        standalone_sender_fn=_standalone_send,
        max_message_length=50_000,
        pii_safe=True,
        emoji="📧",
        allow_update_command=True,
    )
