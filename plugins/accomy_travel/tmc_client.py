"""TMC open-platform HTTP client for the hotel and flight tools.

Every business call is signed (HMAC-SHA256 over the method, path, decoded query, body hash, timestamp,
nonce and app key) and carries a session ``token``. The rules below were pinned by live probes, not by
the docs:

* The signature covers DECODED query values while the URL carries the percent-encoded form. Signing the
  encoded form fails with SIGN_ERROR for non-ASCII keywords such as 東京.
* The body is serialised once and those exact bytes are both signed and sent.
* A missing or expired session token is HTTP 200 with business code "401", not an HTTP 401. The token is
  dropped and the read is sent once more with a fresh one.
* Success is business code "0" (the text "SUCCESS" is accepted too). Anything else is an error.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from datetime import date
from typing import Any, Callable
from urllib.parse import quote

from . import _common

BASE_URL = "https://open.fusionconnectgroup.com"
API_PREFIX = "/openapi/v1/tmc"
APP_KEY_ENV = "TMC_APP_KEY"
APP_SECRET_ENV = "TMC_APP_SECRET"
LANGUAGE = "en-US"

# Every TMC endpoint this bot may reach: the session, lookups, searches, the fares of one itinerary and the
# price check. Orders are recorded in the bot's own database (tools_orders.py). No create-order, pay, cancel,
# change or refund endpoint is listed, so a request for one is refused in _send, whatever key is configured
# and whoever asks.
_READ_ENDPOINTS = frozenset({
    ("POST", "/auth/token"),
    ("GET", "/hotel/locations"),
    ("POST", "/hotel/search"),
    ("POST", "/hotel/products/search"),
    ("GET", "/dict/airports"),
    ("POST", "/flight/search"),
    ("POST", "/flight/cabin"),
    ("POST", "/flight/verify"),
})
_HOTEL_DETAIL_PATH = re.compile(r"/hotel/[0-9]+")  # digits only: /hotel/order is the create-order endpoint
# A flight search asks every fare source at once. With a production key it took about 20 seconds for a route
# nobody had searched lately and returned some 500 fares, so it gets three times the default wait. The price
# check asks the airline itself: for a round trip it took up to 25 seconds, so it gets twice the default, which
# leaves the platform's own 30 seconds as the limit. Every other read answers in a few seconds.
_TIMEOUTS = {
    ("POST", "/flight/search"): 3 * _common.DEFAULT_TIMEOUT,
    ("POST", "/flight/verify"): 2 * _common.DEFAULT_TIMEOUT,
}

_SUCCESS_CODES = frozenset({"0", "SUCCESS"})
_TOKEN_EXPIRED = "401"
# The platform gives a supplier 30 seconds and then answers with this code (HTTP 504).
_DOWNSTREAM_TIMEOUT = "downstream_timeout"
# How often one flight search is sent before the platform's timeout is passed on to the caller.
_SEARCH_ATTEMPTS = 3
# Airport and city lookups change rarely; one cached answer serves every search of the same keyword.
_AIRPORT_TTL_SECONDS = 6 * 3600.0
_AIRPORT_CACHE_MAX = 2048

# A failed mint is remembered this long, so callers queued behind it fail fast instead of minting one by one.
_MINT_FAILURE_TTL_SECONDS = 5.0
_clock: Callable[[], float] = time.monotonic
# Guards the token cache only, so a valid cached token is never queued behind a mint in flight.
_token_lock = threading.Lock()
# Serialises mints: one caller mints, the rest either find its token or its recorded failure.
_mint_lock = threading.Lock()
_tokens: dict[str, str] = {}
_mint_failures: dict[str, tuple[float, _common.ApiError]] = {}
_airport_lock = threading.Lock()
_airport_cache: dict[str, tuple[float, list[dict]]] = {}


def parse_date(value: Any, field: str) -> date:
    """A yyyy-mm-dd date, or ValueError the model can act on."""
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        raise ValueError(
            f"{field} must be a date in yyyy-mm-dd form, for example 2026-12-24."
        ) from None


def canonical_query(params: dict[str, str]) -> tuple[str, str]:
    """(signed, wire) query strings. Keys sort ascending; the wire form percent-encodes spaces as %20."""
    pairs = sorted(params.items())
    signed = "&".join(f"{key}={value}" for key, value in pairs)
    wire = "&".join(
        f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in pairs
    )
    return signed, wire


def sign(
    secret: str,
    *,
    method: str,
    path: str,
    query: str,
    body: bytes,
    timestamp: str,
    nonce: str,
    app_key: str,
) -> str:
    canonical = "\n".join(
        [
            method.upper(),
            path,
            query,
            hashlib.sha256(body).hexdigest(),
            timestamp,
            nonce,
            app_key,
        ]
    )
    return hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _credentials() -> tuple[str, str]:
    app_key, secret = _common.secret(APP_KEY_ENV), _common.secret(APP_SECRET_ENV)
    if not app_key or not secret:
        raise _common.ApiError(
            "not_configured",
            "Hotel and flight search is not configured on this deployment.",
        )
    return app_key, secret


def _send(
    method: str,
    path: str,
    payload: bytes,
    signed_query: str,
    wire_query: str,
    *,
    app_key: str,
    secret: str,
    token: str | None,
) -> tuple[int, Any]:
    if (method, path) not in _READ_ENDPOINTS and not (method == "GET" and _HOTEL_DETAIL_PATH.fullmatch(path)):
        raise _common.ApiError("not_allowed", "This assistant does not send that request to the travel data service.")
    full_path = f"{API_PREFIX}{path}"
    timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "X-OP-App-Key": app_key,
        "X-OP-Timestamp": timestamp,
        "X-OP-Nonce": nonce,
        "X-OP-Sign": sign(
            secret,
            method=method,
            path=full_path,
            query=signed_query,
            body=payload,
            timestamp=timestamp,
            nonce=nonce,
            app_key=app_key,
        ),
    }
    if token:
        headers["token"] = token
    url = f"{BASE_URL}{full_path}" + (f"?{wire_query}" if wire_query else "")
    return _common.request_json(
        method, url, headers=headers, content=payload or None,
        timeout=_TIMEOUTS.get((method, path), _common.DEFAULT_TIMEOUT))


def _settle(status: int, envelope: Any) -> Any:
    """The ``data`` of a success envelope; ApiError for anything else."""
    if not isinstance(envelope, dict):
        raise _common.ApiError(
            "bad_response",
            "The travel data service returned an unexpected answer.",
            status=status,
        )
    code = str(envelope.get("code"))
    if 200 <= status < 300 and code in _SUCCESS_CODES:
        return envelope.get("data")
    if status in (401, 403) and code != _TOKEN_EXPIRED:
        raise _common.ApiError(
            "auth",
            "The travel data service rejected this app's credentials.",
            status=status,
        )
    # Supplier codes are stable labels (for example verify_price_failed); their wording is not shown to the model.
    raise _common.ApiError(
        code.lower(),
        f"The travel data service could not complete the request (code {code}).",
        status=status,
    )


def _mint(app_key: str, secret: str) -> str:
    payload = json.dumps(
        {"externalUserId": app_key}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    status, envelope = _send(
        "POST",
        "/auth/token",
        payload,
        "",
        "",
        app_key=app_key,
        secret=secret,
        token=None,
    )
    token = (_settle(status, envelope) or {}).get("token")
    if not token:
        raise _common.ApiError(
            "auth", "The travel data service did not issue a session.", status=status
        )
    return token


def _session_token(app_key: str, secret: str) -> str:
    """One cached token per app key. Concurrent first calls mint once; during an outage they share the failure."""
    with _token_lock:
        token = _tokens.get(app_key)
    if token is not None:
        return token
    with _mint_lock:
        with _token_lock:
            token = _tokens.get(app_key)
        if token is not None:
            return token
        failure = _mint_failures.get(app_key)
        if failure is not None and _clock() - failure[0] < _MINT_FAILURE_TTL_SECONDS:
            raise _common.ApiError(failure[1].code, failure[1].message, status=failure[1].status)
        try:
            token = _mint(app_key, secret)
        except _common.ApiError as exc:
            _mint_failures[app_key] = (_clock(), exc)
            raise
        with _token_lock:
            _tokens[app_key] = token
        _mint_failures.pop(app_key, None)
        return token


def _drop_token(app_key: str, token: str) -> None:
    with _token_lock:
        if _tokens.get(app_key) == token:
            del _tokens[app_key]


def call(
    method: str, path: str, *, body: Any = None, query: dict[str, str] | None = None
) -> Any:
    """A signed business call; returns the envelope ``data``. A stale session is refreshed once."""
    app_key, secret = _credentials()
    payload = (
        b""
        if body is None
        else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    signed_query, wire_query = canonical_query(query or {})
    for _attempt in range(2):
        token = _session_token(app_key, secret)
        status, envelope = _send(
            method,
            path,
            payload,
            signed_query,
            wire_query,
            app_key=app_key,
            secret=secret,
            token=token,
        )
        if isinstance(envelope, dict) and str(envelope.get("code")) == _TOKEN_EXPIRED:
            _drop_token(app_key, token)
            continue
        return _settle(status, envelope)
    raise _common.ApiError(
        "auth",
        "The travel data service session could not be renewed. Try again shortly.",
    )


def _as_dict(data: Any) -> dict:
    return data if isinstance(data, dict) else {}


def _as_list(data: Any) -> list[dict]:
    return (
        [item for item in data if isinstance(item, dict)]
        if isinstance(data, list)
        else []
    )


# ── hotels ───────────────────────────────────────────────────────────────────


def hotel_locations(keyword: str) -> list[dict]:
    """Destination rows for a keyword. dataType 2 is a city; the rest are hotels and points of interest."""
    return _as_list(call("GET", "/hotel/locations", query={"keyword": keyword}))


def hotel_search(
    *,
    check_in: str,
    check_out: str,
    lat: float,
    lng: float,
    radius_km: int,
    page_size: int,
    page: int,
    star_codes: list[int] | None,
) -> dict:
    """One page of hotels around a point. ``sortBy`` is not honoured by TMC, so callers sort.
    ``totalPage`` tells the caller whether a later page exists."""
    body: dict[str, Any] = {
        "checkInDate": check_in,
        "checkOutDate": check_out,
        "latGoogle": lat,
        "lngGoogle": lng,
        "distance": radius_km,
        "currentPage": page,
        "pageSize": page_size,
        "language": LANGUAGE,
    }
    if star_codes:
        body["hotelStars"] = star_codes
    data = _as_dict(call("POST", "/hotel/search", body=body))
    return {
        "hotels": _as_list(data.get("hotels")),
        "totalCount": data.get("totalCount"),
        "totalPage": data.get("totalPage"),
    }


def hotel_detail(hotel_id: int) -> dict:
    return _as_dict(
        call("GET", f"/hotel/{int(hotel_id)}", query={"language": LANGUAGE})
    )


def hotel_products(
    *, hotel_id: int, check_in: str, check_out: str, adults: int, rooms: int
) -> list[dict]:
    """Room groups with their rate plans. ``numberOfAdults`` is the stay total; TMC splits it across ``roomNum``."""
    body = {
        "checkInDate": check_in,
        "checkOutDate": check_out,
        "hotelId": int(hotel_id),
        "roomNum": rooms,
        "roomGuestNumber": {"numberOfAdults": adults, "roomNumber": rooms},
        "language": LANGUAGE,
    }
    return _as_list(call("POST", "/hotel/products/search", body=body))


# ── flights ──────────────────────────────────────────────────────────────────


def airport_rows(keyword: str) -> list[dict]:
    """City rows (with their airports) for an IATA code or a place name. Cached per keyword; an empty answer
    is not cached, so one transient empty reply cannot hide a valid code for hours."""
    now = time.monotonic()
    with _airport_lock:
        hit = _airport_cache.get(keyword)
    if hit is not None and now - hit[0] < _AIRPORT_TTL_SECONDS:
        return hit[1]
    rows = _as_list(call("GET", "/dict/airports", query={"keyword": keyword}))
    if rows:
        with _airport_lock:
            if len(_airport_cache) >= _AIRPORT_CACHE_MAX:
                _airport_cache.clear()
            _airport_cache[keyword] = (now, rows)
    return rows


def flight_search(body: dict) -> dict:
    """The flights of one leg of a trip, each with its lowest price. ``data`` is null when the supplier holds no
    inventory for the route and date.

    A search of more than one leg asks every direction at once, and the first one for a route can outlast the
    platform's 30 seconds. The supplier keeps what it had found by then, so the same search sent again answers
    in a few seconds: in live probes a round trip nobody had searched answered on the second or third sending.
    A search the platform timed out is therefore sent again, ``_SEARCH_ATTEMPTS`` times in all."""
    request = {**body, "language": LANGUAGE}
    for _attempt in range(_SEARCH_ATTEMPTS - 1):
        try:
            return _as_dict(call("POST", "/flight/search", body=request))
        except _common.ApiError as exc:
            if exc.code != _DOWNSTREAM_TIMEOUT:
                raise
    return _as_dict(call("POST", "/flight/search", body=request))


def flight_fares(price_key: str) -> dict:
    """The fares on sale for one whole itinerary, given the price key a search returned for its last leg: the
    same shape as a search answer, with one flight that holds every leg. ``data`` is null when the itinerary is
    not sold as searched. A round trip or a multi-city trip can be priced live only through one of these fares:
    verify refuses the search's own key for them."""
    return _as_dict(call("POST", "/flight/cabin", body={"priceKey": price_key, "language": LANGUAGE}))


def flight_verify(price_key: str) -> dict:
    """Live re-price of one offer, with its baggage and refund and change rules."""
    return _as_dict(
        call(
            "POST", "/flight/verify", body={"priceKey": price_key, "language": LANGUAGE}
        )
    )
