"""Local authenticated status ingress for gateway-owned routing.

The ingress deliberately accepts no delivery coordinates. External callers present
an opaque handle previously registered by the live gateway; this module resolves
that handle to a gateway-owned origin before dispatching the bounded lifecycle
payload on the gateway event loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import math
import os
import secrets
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

MAX_REQUEST_BYTES = 64 * 1024
MAX_METADATA_BYTES = 4096
MAX_METADATA_KEYS = 32
MAX_ID_LENGTH = 128
MAX_PHASE_LENGTH = 64
MAX_STATUS_LENGTH = 64
MAX_SUMMARY_LENGTH = 256
MAX_ORIGINS = 256

_EVENT_FIELDS = frozenset(
    {
        "activity_kind",
        "activity_id",
        "generation",
        "revision",
        "phase",
        "status",
        "summary",
        "metadata",
    }
)
_REQUEST_FIELDS = frozenset({"token", "origin_handle", "operation", "event"})
_FORBIDDEN_ROUTE_FIELDS = frozenset(
    {
        "chat_id",
        "thread_id",
        "session_key",
        "workspace_id",
        "reply_to_message_id",
        "status_message_id",
        "telegram_message_id",
    }
)
_TERMINAL_PHASES = frozenset({"blocked", "cancelled", "completed", "failed", "ready_for_alex"})


@dataclass(frozen=True)
class OriginRoute:
    """Gateway-owned routing record. Never accepted from an ingress request."""

    platform: str
    chat_id: str
    thread_id: str | None = None
    session_key: str | None = None
    profile: str | None = None
    chat_type: str | None = None
    status_metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OriginRoute":
        return cls(
            platform=str(payload["platform"]),
            chat_id=str(payload["chat_id"]),
            thread_id=_optional_text(payload.get("thread_id")),
            session_key=_optional_text(payload.get("session_key")),
            profile=_optional_text(payload.get("profile")),
            chat_type=_optional_text(payload.get("chat_type")),
            status_metadata=dict(payload.get("status_metadata") or {}),
        )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class StatusOriginRegistry:
    """Small persisted routing registry; contains no card or lifecycle state."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "origins.json"
        _ensure_private_directory(self.root)
        self._routes = self._load()

    def _load(self) -> dict[str, OriginRoute]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return {}
        raw_routes = payload.get("origins")
        if not isinstance(raw_routes, dict):
            return {}
        routes: dict[str, OriginRoute] = {}
        for handle, raw_route in raw_routes.items():
            if len(routes) >= MAX_ORIGINS:
                break
            try:
                if not str(handle).startswith("origin_") or not isinstance(raw_route, dict):
                    continue
                routes[str(handle)] = OriginRoute.from_dict(raw_route)
            except (KeyError, TypeError, ValueError):
                continue
        return routes

    def _save(self) -> None:
        _atomic_json(
            self.path,
            {
                "version": 1,
                "origins": {
                    handle: asdict(route)
                    for handle, route in sorted(self._routes.items())
                },
            },
        )

    def register(self, route: OriginRoute) -> str:
        if not route.platform.strip() or not route.chat_id.strip():
            raise ValueError("origin route requires platform and chat_id")
        for handle, existing in self._routes.items():
            if self._route_identity(existing) == self._route_identity(route):
                if existing != route:
                    self._routes[handle] = route
                    self._save()
                return handle
        handle = f"origin_{secrets.token_urlsafe(24)}"
        if len(self._routes) >= MAX_ORIGINS:
            self._routes.pop(next(iter(self._routes)))
        self._routes[handle] = route
        self._save()
        return handle

    @staticmethod
    def _route_identity(route: OriginRoute) -> tuple[Any, ...]:
        return (
            route.platform,
            route.chat_id,
            route.thread_id,
            route.session_key,
            route.profile,
            route.chat_type,
        )

    def resolve(self, handle: str) -> OriginRoute | None:
        return self._routes.get(str(handle or ""))


def _bounded_text(payload: dict[str, Any], field_name: str, limit: int, *, required: bool = True) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        if required:
            raise ValueError(f"{field_name} must be text")
        return ""
    text = value.strip()
    if required and not text:
        raise ValueError(f"{field_name} is required")
    if len(text) > limit:
        raise ValueError(f"{field_name} exceeds {limit} characters")
    return text


def _validate_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_METADATA_KEYS:
        raise ValueError("metadata must be a bounded object")
    _validate_metadata_value(value, depth=0)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be JSON-compatible") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds 4096 bytes")
    return json.loads(encoded)


def _validate_metadata_value(value: Any, *, depth: int) -> None:
    if depth > 4:
        raise ValueError("metadata exceeds nesting limit")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValueError("metadata contains an invalid key")
            if key in _FORBIDDEN_ROUTE_FIELDS:
                raise ValueError("metadata contains a forbidden routing key")
            _validate_metadata_value(child, depth=depth + 1)
        return
    if isinstance(value, list):
        for child in value:
            _validate_metadata_value(child, depth=depth + 1)
        return
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    raise ValueError("metadata contains an unsupported value")


def validate_status_event(raw: Any) -> dict[str, Any]:
    """Validate the complete caller-controlled lifecycle envelope."""

    if not isinstance(raw, dict):
        raise ValueError("event must be an object")
    unsupported = sorted(set(raw) - _EVENT_FIELDS)
    if unsupported:
        raise ValueError(f"unsupported field: {unsupported[0]}")
    revision = raw.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("revision must be a positive integer")
    return {
        "activity_kind": _bounded_text(raw, "activity_kind", 64),
        "activity_id": _bounded_text(raw, "activity_id", MAX_ID_LENGTH),
        "generation": _bounded_text(raw, "generation", MAX_ID_LENGTH),
        "revision": revision,
        "phase": _bounded_text(raw, "phase", MAX_PHASE_LENGTH),
        "status": _bounded_text(raw, "status", MAX_STATUS_LENGTH),
        "summary": _bounded_text(raw, "summary", MAX_SUMMARY_LENGTH, required=False),
        "metadata": _validate_metadata(raw.get("metadata")),
    }


def event_is_terminal(event: dict[str, Any]) -> bool:
    return str(event.get("phase") or "").lower() in _TERMINAL_PHASES


EventCallback = Callable[[OriginRoute, dict[str, Any]], Any | Awaitable[Any]]


def _status_socket_path(root: Path) -> Path:
    candidate = root / "status.sock"
    if len(os.fsencode(candidate)) <= 100:
        return candidate
    digest = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:16]
    return (
        Path(tempfile.gettempdir())
        / f"hermes-status-{os.getuid()}-{digest}"
        / "status.sock"
    )


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("status ingress directory must not be a symlink")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("status ingress directory ownership is unsafe")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("status ingress directory permissions are unsafe")


class GatewayStatusIngress:
    """Authenticated Unix-socket status ingress owned by one gateway process."""

    def __init__(self, root: Path, *, on_event: EventCallback):
        self.root = Path(root)
        self.socket_path = _status_socket_path(self.root)
        self.token_path = self.root / "token"
        self.registry = StatusOriginRegistry(self.root)
        self.on_event = on_event
        self._server: asyncio.AbstractServer | None = None
        self._token: str | None = None

    def register_origin(self, route: OriginRoute) -> str:
        return self.registry.register(route)

    def _load_or_create_token(self) -> str:
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = ""
        if len(token) < 32:
            token = secrets.token_urlsafe(32)
            fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        os.chmod(self.token_path, 0o600)
        return token

    async def start(self) -> None:
        if self._server is not None:
            return
        _ensure_private_directory(self.root)
        _ensure_private_directory(self.socket_path.parent)
        self._token = self._load_or_create_token()
        try:
            mode = self.socket_path.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(mode):
                raise RuntimeError("status ingress path exists and is not a socket")
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
            limit=MAX_REQUEST_BYTES + 1,
        )
        os.chmod(self.socket_path, 0o600)

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    async def _reply(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        writer.write(json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n")
        await writer.drain()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                raw = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError):
                await self._reply(writer, {"ok": False, "error": "request_too_large"})
                return
            if len(raw) > MAX_REQUEST_BYTES:
                await self._reply(writer, {"ok": False, "error": "request_too_large"})
                return
            try:
                request = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                await self._reply(writer, {"ok": False, "error": "invalid_json"})
                return
            if not isinstance(request, dict) or set(request) - _REQUEST_FIELDS:
                await self._reply(writer, {"ok": False, "error": "invalid_request"})
                return
            supplied_token = request.get("token")
            if not isinstance(supplied_token, str) or self._token is None or not hmac.compare_digest(supplied_token, self._token):
                await self._reply(writer, {"ok": False, "error": "unauthorized"})
                return
            route = self.registry.resolve(str(request.get("origin_handle") or ""))
            if route is None:
                await self._reply(writer, {"ok": False, "error": "unknown_origin_handle"})
                return
            operation = request.get("operation")
            if operation == "health" and "event" not in request:
                await self._reply(writer, {"ok": True, "result": {"healthy": True}})
                return
            if operation is not None:
                await self._reply(writer, {"ok": False, "error": "invalid_request"})
                return
            try:
                event = validate_status_event(request.get("event"))
            except ValueError:
                await self._reply(writer, {"ok": False, "error": "invalid_event"})
                return
            try:
                result = self.on_event(route, event)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                await self._reply(writer, {"ok": False, "error": "dispatch_failed"})
                return
            await self._reply(writer, {"ok": True, "result": result})
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, RuntimeError):
                pass
