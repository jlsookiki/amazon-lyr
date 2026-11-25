"""Shared client accessor for tools. One client per process; cookies loaded lazily.

With per-user sessions on (an HTTP transport behind a bearer token, fronted by a
gate that authenticates people), each request names its user in USER_HEADER and
gets a client built from that user's own stored session.
"""

from __future__ import annotations

import os
import re

from amazon_lyr import constants
from amazon_lyr.constants import REQUEST_INTERVAL
from amazon_lyr.exceptions import AuthenticationError
from amazon_lyr.marketplace import resolve_marketplace
from amazon_lyr.ratelimit import RateLimiter
from amazon_lyr.scraping.api_client import AmazonClient
from amazon_lyr.session_state import get_user_cookies_path, load_cookies, read_cookies_file

USER_HEADER = "x-amazon-lyr-user"
_USER_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

_client: AmazonClient | None = None
_per_user = False
# user -> (cookie file mtime, client); rebuilt when the user signs in again.
_user_clients: dict[str, tuple[float, AmazonClient]] = {}
# Everyone shares one IP, so everyone shares one request cadence.
_shared_limiter = RateLimiter(min_interval=REQUEST_INTERVAL)


def enable_per_user_sessions() -> None:
    global _per_user
    _per_user = True


def _signin_hint() -> str:
    url = os.getenv("AMAZON_LYR_SIGNIN_URL", "").strip()
    return f" Connect Amazon at {url}" if url else ""


def _request_user() -> str:
    from fastmcp.server.dependencies import get_http_headers  # noqa: PLC0415

    user = get_http_headers().get(USER_HEADER, "")
    if not _USER_RE.fullmatch(user):
        raise AuthenticationError("This request names no user, so no Amazon session is used.")
    return user


def _user_client(user: str) -> AmazonClient:
    path = get_user_cookies_path(user)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        raise AuthenticationError(
            "This account has no Amazon session yet." + _signin_hint()
        ) from None
    cached = _user_clients.get(user)
    if cached and cached[0] == mtime:
        return cached[1]
    cookies = read_cookies_file(path)
    marketplace = resolve_marketplace(cookies)
    if marketplace.login_cookie not in cookies:
        raise AuthenticationError("This account's Amazon session is incomplete." + _signin_hint())
    client = AmazonClient(cookies=cookies, marketplace=marketplace.base_url)
    client._rate_limiter = _shared_limiter
    _user_clients[user] = (mtime, client)
    return client


def session_cookies() -> dict[str, str]:
    """The cookies the current request would use (for status reporting)."""
    if _per_user:
        path = get_user_cookies_path(_request_user())
        return read_cookies_file(path) if path.exists() else {}
    return load_cookies()


def get_amazon_client() -> AmazonClient:
    global _client
    if constants.RESEARCH_ONLY and not constants.RESEARCH_SIGNED_IN:
        if _client is None:
            # Signed-out by design: never read or send stored cookies.
            _client = AmazonClient(cookies=None, marketplace=resolve_marketplace(None).base_url)
        return _client
    if _per_user:
        return _user_client(_request_user())
    if _client is None:
        cookies = load_cookies()
        marketplace = resolve_marketplace(cookies)
        if marketplace.login_cookie not in cookies:
            raise AuthenticationError(
                f"No {marketplace.domain} login cookie ({marketplace.login_cookie}) found. "
                "Run `amazon-lyr login --cookies-file` first (or set the AMAZON_COOKIES "
                "env var with your session JSON)."
            )
        _client = AmazonClient(cookies=cookies, marketplace=marketplace.base_url)
    return _client


def reset_client() -> None:
    """Drop the cached clients (after cookie re-import)."""
    global _client, _per_user
    _client = None
    _per_user = False
    _user_clients.clear()
