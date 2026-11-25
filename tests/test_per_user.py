"""Per-user sessions: a token-holding gate names the user in a header and each
user's own stored session is used (see dependencies.enable_per_user_sessions)."""

import asyncio
import json
import os
import re
import time

import pytest
from conftest import FakeResponse, FakeSession
from starlette.testclient import TestClient

from amazon_lyr import cli_main, constants, dependencies
from amazon_lyr.error_handler import handle_api_error
from amazon_lyr.exceptions import AuthenticationError, SessionExpiredError
from amazon_lyr.ratelimit import RateLimiter
from amazon_lyr.scraping.api_client import AmazonClient
from amazon_lyr.server import create_mcp_server
from amazon_lyr.session_state import get_user_cookies_path

pytestmark = pytest.mark.research_only

TOKEN = "t" * constants.MIN_AUTH_TOKEN_LENGTH


def _store(user, token):
    path = get_user_cookies_path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at-main": token, "session-id": "1", "ubid-main": "2"}))
    return path


@pytest.fixture
def per_user(isolated_config, monkeypatch):
    dependencies.enable_per_user_sessions()
    headers = {}
    monkeypatch.setattr("fastmcp.server.dependencies.get_http_headers", lambda **_: headers)
    return headers


def test_each_user_gets_their_own_session(per_user):
    _store("alice", "alice-token")
    _store("bob", "bob-token")
    per_user[dependencies.USER_HEADER] = "alice"
    alice = dependencies.get_amazon_client()
    per_user[dependencies.USER_HEADER] = "bob"
    bob = dependencies.get_amazon_client()
    assert alice._cookies["at-main"] == "alice-token"
    assert bob._cookies["at-main"] == "bob-token"
    assert alice._rate_limiter is bob._rate_limiter


def test_default_session_is_never_used(per_user, monkeypatch):
    monkeypatch.setenv("AMAZON_COOKIES", json.dumps({"at-main": "owner", "session-id": "1"}))
    with pytest.raises(AuthenticationError, match="names no user"):
        dependencies.get_amazon_client()


@pytest.mark.parametrize("user", ["", "../alice", "Alice", "a/b", "a" * 40])
def test_malformed_user_is_refused(per_user, user):
    _store("alice", "alice-token")
    per_user[dependencies.USER_HEADER] = user
    with pytest.raises(AuthenticationError):
        dependencies.get_amazon_client()


def test_user_without_session_gets_signin_link(per_user, monkeypatch):
    monkeypatch.setenv("AMAZON_LYR_SIGNIN_URL", "https://example.test/connect")
    per_user[dependencies.USER_HEADER] = "carol"
    with pytest.raises(AuthenticationError, match="https://example.test/connect"):
        dependencies.get_amazon_client()


def test_signing_in_again_rebuilds_the_client(per_user):
    path = _store("alice", "old")
    per_user[dependencies.USER_HEADER] = "alice"
    first = dependencies.get_amazon_client()
    path.write_text(json.dumps({"at-main": "new", "session-id": "1"}))
    later = time.time() + 5

    os.utime(path, (later, later))
    assert dependencies.get_amazon_client() is not first
    assert dependencies.get_amazon_client()._cookies["at-main"] == "new"


def test_per_user_needs_a_token(monkeypatch):
    monkeypatch.setenv("AMAZON_LYR_PER_USER", "1")
    monkeypatch.delenv("AMAZON_LYR_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("amazon_lyr.server.create_mcp_server", lambda **_: None)
    with pytest.raises(SystemExit, match="AMAZON_LYR_AUTH_TOKEN"):
        cli_main.main(["serve", "--transport", "streamable-http"])


async def test_shared_limiter_spaces_concurrent_callers():
    limiter = RateLimiter(min_interval=0.05)
    start = time.monotonic()
    await asyncio.gather(*(limiter.acquire() for _ in range(3)))
    assert time.monotonic() - start >= 0.1


HEADERS = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {TOKEN}"}


def _rpc(http, body, sid=None, user=None):
    headers = dict(HEADERS)
    if sid:
        headers["mcp-session-id"] = sid
    if user:
        headers[dependencies.USER_HEADER] = user
    return http.post("/mcp", json=body, headers=headers)


def test_header_selects_the_session_over_http(isolated_config, monkeypatch):
    """End to end through the HTTP transport: the header picks whose cookies go out."""
    _store("alice", "alice-token")
    _store("bob", "bob-token")
    dependencies.enable_per_user_sessions()
    sent = []

    async def fake_session(self):
        sent.append(self._cookies["at-main"])
        return FakeSession([FakeResponse("<html></html>")])

    monkeypatch.setattr(AmazonClient, "_get_session", fake_session)
    app = create_mcp_server(auth_token=TOKEN).http_app(path="/mcp")
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    with TestClient(app) as http:
        sid = _rpc(http, init).headers["mcp-session-id"]
        _rpc(http, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        for i, user in enumerate(["bob", "alice"], start=2):
            call = {
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": {"name": "search_products", "arguments": {"query": "hub"}},
            }
            assert _rpc(http, call, sid, user).status_code == 200
    assert sent == ["bob-token", "alice-token"]


@pytest.mark.parametrize("value", ["true", "yes", "1 ", "on", "2"])
def test_unrecognised_per_user_value_refuses_to_start(monkeypatch, value):
    monkeypatch.setenv("AMAZON_LYR_PER_USER", value)
    monkeypatch.setenv("AMAZON_LYR_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr("amazon_lyr.server.create_mcp_server", lambda **_: None)
    with pytest.raises(SystemExit, match="AMAZON_LYR_PER_USER"):
        cli_main.main(["serve", "--transport", "streamable-http"])


@pytest.mark.parametrize(("value", "on"), [("", False), ("0", False), ("1", True)])
def test_per_user_values(isolated_config, monkeypatch, value, on):
    monkeypatch.setenv("AMAZON_LYR_PER_USER", value)
    monkeypatch.setenv("AMAZON_LYR_AUTH_TOKEN", TOKEN)

    class FakeMCP:
        def run(self, **_):
            pass

    monkeypatch.setattr("amazon_lyr.server.create_mcp_server", lambda **_: FakeMCP())
    cli_main.main(["serve", "--transport", "streamable-http"])
    assert dependencies._per_user is on


class _ClosableSession:
    closed = False

    async def close(self):
        self.closed = True


async def test_replaced_client_is_closed(per_user):
    path = _store("alice", "old")
    per_user[dependencies.USER_HEADER] = "alice"
    old = dependencies.get_amazon_client()
    old._session = session = _ClosableSession()
    path.write_text(json.dumps({"at-main": "new", "session-id": "1"}))
    os.utime(path, (time.time() + 5, time.time() + 5))
    dependencies.get_amazon_client()
    await asyncio.sleep(0)
    assert session.closed


async def test_shutdown_closes_every_user_client(per_user):
    sessions = []
    for user in ("alice", "bob"):
        _store(user, user)
        per_user[dependencies.USER_HEADER] = user
        client = dependencies.get_amazon_client()
        client._session = s = _ClosableSession()
        sessions.append(s)
    await dependencies.close_clients()
    assert all(s.closed for s in sessions)
    assert dependencies._user_clients == {}


def test_status_reports_the_requesting_users_session(per_user, monkeypatch):
    monkeypatch.setenv("AMAZON_COOKIES", json.dumps({"at-main": "owner", "session-id": "1"}))
    _store("alice", "alice-token")
    per_user[dependencies.USER_HEADER] = "alice"
    assert dependencies.session_cookies()["at-main"] == "alice-token"
    per_user[dependencies.USER_HEADER] = "bob"
    assert dependencies.session_cookies() == {}
    per_user[dependencies.USER_HEADER] = ""
    with pytest.raises(AuthenticationError):
        dependencies.session_cookies()


def test_session_without_login_cookie_is_refused(per_user):
    path = get_user_cookies_path("alice")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"session-id": "1"}))
    per_user[dependencies.USER_HEADER] = "alice"
    with pytest.raises(AuthenticationError, match="incomplete"):
        dependencies.get_amazon_client()


@pytest.mark.parametrize(
    ("url", "hint"),
    [
        ("https://example.test/account", "reconnect Amazon at https://example.test/account"),
        ("", "amazon-lyr --login"),
    ],
)
def test_expired_session_hint(monkeypatch, url, hint):
    monkeypatch.setenv("AMAZON_LYR_SIGNIN_URL", url)
    with pytest.raises(RuntimeError, match=re.escape(hint)):
        handle_api_error(SessionExpiredError("Amazon redirected to sign-in"))
