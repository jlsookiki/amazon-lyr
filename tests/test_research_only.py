"""Research-only build: signed-out reads, six tools, token-protected HTTP.

These tests run against the shipped ``RESEARCH_ONLY`` constant (see the
``research_only`` marker in conftest).
"""

import asyncio
import json

import pytest
from conftest import FakeResponse, FakeSession, attach_session
from fastmcp import Client
from starlette.testclient import TestClient

from amazon_lyr import cli_main, constants, dependencies
from amazon_lyr.server import create_mcp_server

pytestmark = pytest.mark.research_only

RESEARCH_TOOLS = {
    "search_products",
    "get_product",
    "compare_products",
    "get_reviews",
    "review_insights",
    "search_in_reviews",
}
TOKEN = "t" * constants.MIN_AUTH_TOKEN_LENGTH

SEARCH_HTML = """
<div data-component-type="s-search-result" data-asin="B000000001">
  <h2><span>USB-C Hub</span></h2>
  <span class="a-price"><span class="a-offscreen">$34.99</span></span>
</div>
"""


def test_shipped_constant_is_on():
    assert constants.RESEARCH_ONLY is True


def test_only_research_tools_are_registered():
    names = {t.name for t in asyncio.run(create_mcp_server().list_tools())}
    assert names == RESEARCH_TOOLS


def test_instructions_describe_signed_out_research():
    text = create_mcp_server().instructions
    assert "signed out" in text
    assert "add_to_cart" not in text and "get_orders" not in text


async def test_search_works_without_cookies(isolated_config):
    client = dependencies.get_amazon_client()
    fake = FakeSession([FakeResponse(SEARCH_HTML)])
    attach_session(client, fake)
    async with Client(create_mcp_server()) as mcp:
        result = await mcp.call_tool("search_products", {"query": "usb hub"})
    assert result.data["results"][0]["asin"] == "B000000001"


def test_stored_cookies_are_never_loaded(isolated_config, monkeypatch):
    monkeypatch.setenv("AMAZON_COOKIES", json.dumps({"at-main": "secret", "session-id": "1"}))
    client = dependencies.get_amazon_client()
    assert client._cookies == {}


def _serve(monkeypatch, argv, **env):
    seen = {}

    class FakeMCP:
        def run(self, **kwargs):
            seen["run"] = kwargs

    def fake_create(auth_token=None):
        if auth_token is not None and len(auth_token) < constants.MIN_AUTH_TOKEN_LENGTH:
            raise ValueError("too short")
        seen["token"] = auth_token
        return FakeMCP()

    monkeypatch.setattr("amazon_lyr.server.create_mcp_server", fake_create)
    for key in ("AMAZON_LYR_AUTH_TOKEN", "HOST", "TRANSPORT", "PORT", "HTTP_PATH"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cli_main.main(argv)
    return seen


HTTP = ["serve", "--transport", "streamable-http"]


def test_non_loopback_refused_without_token(monkeypatch):
    with pytest.raises(SystemExit) as e:
        _serve(monkeypatch, [*HTTP, "--host", "0.0.0.0"])
    assert "AMAZON_LYR_AUTH_TOKEN" in str(e.value)


def test_non_loopback_allowed_with_token(monkeypatch):
    seen = _serve(monkeypatch, [*HTTP, "--host", "0.0.0.0"], AMAZON_LYR_AUTH_TOKEN=TOKEN)
    assert seen["token"] == TOKEN
    assert seen["run"]["host"] == "0.0.0.0"


def test_short_token_refused(monkeypatch):
    with pytest.raises(SystemExit):
        _serve(monkeypatch, HTTP, AMAZON_LYR_AUTH_TOKEN="short")


def test_loopback_without_token_still_serves(monkeypatch):
    seen = _serve(monkeypatch, HTTP)
    assert seen["token"] is None


def test_create_server_rejects_short_token():
    with pytest.raises(ValueError):
        create_mcp_server(auth_token="short")


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream"}


def _post(headers):
    app = create_mcp_server(auth_token=TOKEN).http_app(path="/mcp")
    with TestClient(app) as http:
        return http.post("/mcp", json=INIT, headers={**HEADERS, **headers})


def test_http_rejects_missing_token():
    assert _post({}).status_code == 401


def test_http_rejects_wrong_token():
    assert _post({"Authorization": "Bearer " + "x" * 40}).status_code == 401


def test_http_accepts_right_token():
    assert _post({"Authorization": f"Bearer {TOKEN}"}).status_code == 200
