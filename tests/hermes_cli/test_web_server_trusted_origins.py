"""Regression tests for reverse-proxy WebSocket Origin allowlisting."""

from types import SimpleNamespace

import pytest

from hermes_cli import web_server


def _ws(*, origin: str, host: str = "127.0.0.1:9119"):
    return SimpleNamespace(headers={"host": host, "origin": origin})


def test_normalizes_only_strict_http_origins():
    assert web_server._normalize_dashboard_trusted_origins(
        [
            "https://Hermes.Acolod.com",
            "http://localhost:3000",
            "https://hermes.acolod.com/",
            "https://hermes.acolod.com?",
            "https://hermes.acolod.com#",
            "ftp://hermes.acolod.com",
            "https://user:pass@hermes.acolod.com",
            "https://hermes.acolod.com/path",
            "https://hermes.acolod.com?query=yes",
            "not-a-url",
            123,
        ]
    ) == frozenset(
        {
            "https://hermes.acolod.com",
            "http://localhost:3000",
        }
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://hermes.acolod.com/",
        "https://hermes.acolod.com?",
        "https://hermes.acolod.com#",
    ],
)
def test_rejects_non_bare_origin_delimiters(value):
    assert web_server._normalize_dashboard_trusted_origins([value]) == frozenset()


def test_loopback_proxy_accepts_exact_trusted_public_origin(monkeypatch):
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(
        web_server.app.state,
        "trusted_origins",
        frozenset({"https://hermes.acolod.com"}),
        raising=False,
    )

    reason = web_server._ws_host_origin_reason(
        _ws(origin="https://hermes.acolod.com")
    )

    assert reason is None


def test_trusted_origin_match_is_scheme_and_port_exact(monkeypatch):
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(
        web_server.app.state,
        "trusted_origins",
        frozenset({"https://hermes.acolod.com"}),
        raising=False,
    )

    assert "origin_mismatch" in web_server._ws_host_origin_reason(
        _ws(origin="http://hermes.acolod.com")
    )
    assert "origin_mismatch" in web_server._ws_host_origin_reason(
        _ws(origin="https://hermes.acolod.com:8443")
    )


def test_untrusted_cross_site_origin_remains_rejected(monkeypatch):
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(
        web_server.app.state,
        "trusted_origins",
        frozenset({"https://hermes.acolod.com"}),
        raising=False,
    )

    reason = web_server._ws_host_origin_reason(_ws(origin="https://evil.test"))

    assert reason == (
        "origin_mismatch origin=https://evil.test bound=127.0.0.1"
    )


def test_trusted_origin_does_not_bypass_host_header_guard(monkeypatch):
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)
    monkeypatch.setattr(
        web_server.app.state,
        "trusted_origins",
        frozenset({"https://hermes.acolod.com"}),
        raising=False,
    )

    reason = web_server._ws_host_origin_reason(
        _ws(origin="https://hermes.acolod.com", host="evil.test")
    )

    assert reason == "host_mismatch host=evil.test bound=127.0.0.1"
