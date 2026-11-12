"""Contracts of the TMC open-platform client: signing, session tokens, envelope handling and caching.

Every HTTP exchange goes through ``_common.request_json``, which a fake replaces, so nothing leaves the process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

import pytest

from plugins.accomy_travel import _common, tmc_client

APP_KEY = "app-key-for-tests"
SECRET = "secret-for-tests"
TOKEN_PATH = "/openapi/v1/tmc/auth/token"


@dataclass
class Exchange:
    method: str
    url: str
    headers: dict
    content: bytes | None


class FakeTmc:
    """Routes ``(method, path)`` to a responder; responders return ``(status, body)`` and may be callables."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.exchanges: list[Exchange] = []

    def __call__(
        self,
        method,
        url,
        *,
        headers=None,
        params=None,
        json_body=None,
        content=None,
        timeout=None,
    ):
        exchange = Exchange(method, url, dict(headers or {}), content)
        self.exchanges.append(exchange)
        path = url.split("?", 1)[0].removeprefix(tmc_client.BASE_URL)
        responder = self.routes[(method, path)]
        return responder(exchange) if callable(responder) else responder

    def paths(self) -> list[str]:
        return [
            exchange.url.split("?", 1)[0].removeprefix(tmc_client.BASE_URL)
            for exchange in self.exchanges
        ]


def _token_ok(token: str = "session-1") -> tuple[int, dict]:
    return 200, {"code": "0", "message": "success", "data": {"token": token}}


@pytest.fixture(autouse=True)
def _client_state(monkeypatch):
    tmc_client._tokens.clear()
    tmc_client._mint_failures.clear()
    tmc_client._airport_cache.clear()
    monkeypatch.setattr(
        _common,
        "secret",
        lambda name: {"TMC_APP_KEY": APP_KEY, "TMC_APP_SECRET": SECRET}.get(name, ""),
    )
    monkeypatch.setattr(_common, "caller_id", lambda: "user-a")
    yield
    tmc_client._tokens.clear()
    tmc_client._mint_failures.clear()
    tmc_client._airport_cache.clear()


def _install(monkeypatch, routes: dict[tuple[str, str], Any]) -> FakeTmc:
    fake = FakeTmc({("POST", TOKEN_PATH): _token_ok(), **routes})
    monkeypatch.setattr(_common, "request_json", fake)
    return fake


def test_canonical_query_signs_decoded_values_and_encodes_the_wire_form():
    signed, wire = tmc_client.canonical_query({"keyword": "東京", "language": "en-US"})
    assert signed == "keyword=東京&language=en-US"
    assert wire == "keyword=%E6%9D%B1%E4%BA%AC&language=en-US"


def test_canonical_query_encodes_spaces_as_percent_twenty():
    _signed, wire = tmc_client.canonical_query({"keyword": "New York"})
    assert wire == "keyword=New%20York"


def test_signature_is_hmac_over_the_documented_canonical_lines():
    body = b'{"a":1}'
    signature = tmc_client.sign(
        SECRET,
        method="post",
        path="/openapi/v1/tmc/hotel/search",
        query="b=2&c=3",
        body=body,
        timestamp="1700000000",
        nonce="abc",
        app_key=APP_KEY,
    )
    canonical = "\n".join(
        [
            "POST",
            "/openapi/v1/tmc/hotel/search",
            "b=2&c=3",
            hashlib.sha256(body).hexdigest(),
            "1700000000",
            "abc",
            APP_KEY,
        ]
    )
    expected = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    assert signature == expected


def test_business_call_signs_the_exact_bytes_it_sends_and_carries_the_session(
    monkeypatch,
):
    routes = {
        ("POST", "/openapi/v1/tmc/hotel/search"): (
            200,
            {"code": "0", "data": {"hotels": []}},
        )
    }
    fake = _install(monkeypatch, routes)

    data = tmc_client.call(
        "POST", "/hotel/search", body={"keyword": "東京"}, query={"keyword": "東京 塔"}
    )

    assert data == {"hotels": []}
    exchange = fake.exchanges[-1]
    assert exchange.content == '{"keyword":"東京"}'.encode()
    assert exchange.url.endswith("?keyword=%E6%9D%B1%E4%BA%AC%20%E5%A1%94")
    assert exchange.headers["token"] == "session-1"
    assert exchange.headers["X-OP-App-Key"] == APP_KEY
    canonical = "\n".join(
        [
            "POST",
            "/openapi/v1/tmc/hotel/search",
            "keyword=東京 塔",
            hashlib.sha256(exchange.content).hexdigest(),
            exchange.headers["X-OP-Timestamp"],
            exchange.headers["X-OP-Nonce"],
            APP_KEY,
        ]
    )
    assert (
        exchange.headers["X-OP-Sign"]
        == hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    )


def test_session_token_is_minted_once_and_reused(monkeypatch):
    routes = {
        ("GET", "/openapi/v1/tmc/dict/airports"): (200, {"code": "0", "data": []})
    }
    fake = _install(monkeypatch, routes)

    tmc_client.call("GET", "/dict/airports", query={"keyword": "a"})
    tmc_client.call("GET", "/dict/airports", query={"keyword": "b"})

    assert fake.paths().count(TOKEN_PATH) == 1


def test_a_failed_mint_is_shared_for_a_few_seconds_instead_of_minted_again(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(tmc_client, "_clock", lambda: now[0])
    outage = {"down": True}

    def auth_endpoint(exchange):
        if outage["down"]:
            return 503, {"code": "5030", "message": "down"}
        return _token_ok()

    routes = {
        ("GET", "/openapi/v1/tmc/dict/airports"): (200, {"code": "0", "data": []}),
        ("POST", TOKEN_PATH): auth_endpoint,
    }
    fake = _install(monkeypatch, routes)

    with pytest.raises(_common.ApiError) as first:
        tmc_client.call("GET", "/dict/airports", query={"keyword": "a"})
    with pytest.raises(_common.ApiError) as waiter:
        tmc_client.call("GET", "/dict/airports", query={"keyword": "b"})

    assert waiter.value.code == first.value.code == "5030"
    assert fake.paths().count(TOKEN_PATH) == 1

    outage["down"] = False
    now[0] += tmc_client._MINT_FAILURE_TTL_SECONDS
    assert tmc_client.call("GET", "/dict/airports", query={"keyword": "c"}) == []
    assert fake.paths().count(TOKEN_PATH) == 2


def test_token_request_names_the_app_key_as_external_user(monkeypatch):
    routes = {
        ("GET", "/openapi/v1/tmc/dict/airports"): (200, {"code": "0", "data": []})
    }
    fake = _install(monkeypatch, routes)

    tmc_client.call("GET", "/dict/airports", query={"keyword": "a"})

    mint = fake.exchanges[0]
    assert json.loads(mint.content) == {"externalUserId": APP_KEY}
    assert "token" not in mint.headers


def test_expired_session_is_refreshed_once_and_the_read_is_resent(monkeypatch):
    minted = iter(["session-old", "session-new"])
    seen_tokens: list[str] = []

    def business(exchange: Exchange):
        seen_tokens.append(exchange.headers["token"])
        if exchange.headers["token"] == "session-old":
            return 200, {"code": "401", "message": "expired"}
        return 200, {"code": "0", "data": {"ok": True}}

    routes = {
        ("POST", TOKEN_PATH): lambda _exchange: _token_ok(next(minted)),
        ("GET", "/openapi/v1/tmc/dict/airports"): business,
    }
    _install(monkeypatch, routes)

    assert tmc_client.call("GET", "/dict/airports", query={"keyword": "x"}) == {
        "ok": True
    }
    assert seen_tokens == ["session-old", "session-new"]


def test_a_second_expired_answer_is_an_auth_error(monkeypatch):
    routes = {
        ("POST", TOKEN_PATH): _token_ok(),
        ("GET", "/openapi/v1/tmc/dict/airports"): (
            200,
            {"code": "401", "message": "expired"},
        ),
    }
    _install(monkeypatch, routes)

    with pytest.raises(_common.ApiError) as raised:
        tmc_client.call("GET", "/dict/airports", query={"keyword": "x"})
    assert raised.value.code == "auth"


@pytest.mark.parametrize("success_code", ["0", "SUCCESS"])
def test_success_envelope_codes_return_data(monkeypatch, success_code):
    _install(
        monkeypatch,
        {
            ("GET", "/openapi/v1/tmc/dict/airports"): (
                200,
                {"code": success_code, "data": [1]},
            )
        },
    )
    assert tmc_client.call("GET", "/dict/airports", query={"keyword": "x"}) == [1]


def test_supplier_error_code_becomes_a_stable_lowercase_label(monkeypatch):
    _install(
        monkeypatch,
        {
            ("GET", "/openapi/v1/tmc/dict/airports"): (
                200,
                {
                    "code": "PARAM_VALIDATION_ERROR",
                    "message": "Destination city does not exist",
                },
            )
        },
    )

    with pytest.raises(_common.ApiError) as raised:
        tmc_client.call("GET", "/dict/airports", query={"keyword": "x"})
    assert raised.value.code == "param_validation_error"
    assert "PARAM_VALIDATION_ERROR" in raised.value.message


def test_gateway_signature_rejection_is_reported_as_auth(monkeypatch):
    _install(
        monkeypatch,
        {
            ("GET", "/openapi/v1/tmc/dict/airports"): (
                401,
                {"code": "SIGN_ERROR", "message": "bad"},
            )
        },
    )

    with pytest.raises(_common.ApiError) as raised:
        tmc_client.call("GET", "/dict/airports", query={"keyword": "x"})
    assert raised.value.code == "auth"


def test_an_answer_that_is_not_an_envelope_is_a_bad_response(monkeypatch):
    _install(
        monkeypatch, {("GET", "/openapi/v1/tmc/dict/airports"): (200, ["unexpected"])}
    )

    with pytest.raises(_common.ApiError) as raised:
        tmc_client.call("GET", "/dict/airports", query={"keyword": "x"})
    assert raised.value.code == "bad_response"


def test_missing_credentials_refuse_before_any_request(monkeypatch):
    fake = _install(monkeypatch, {})
    monkeypatch.setattr(_common, "secret", lambda name: "")

    with pytest.raises(_common.ApiError) as raised:
        tmc_client.call("GET", "/dict/airports", query={"keyword": "x"})
    assert raised.value.code == "not_configured"
    assert fake.exchanges == []


def test_airport_lookup_is_cached_per_keyword(monkeypatch):
    rows = [{"cityCode": "TPE", "airports": []}]
    fake = _install(
        monkeypatch,
        {("GET", "/openapi/v1/tmc/dict/airports"): (200, {"code": "0", "data": rows})},
    )

    assert tmc_client.airport_rows("TPE") == rows
    assert tmc_client.airport_rows("TPE") == rows
    assert fake.paths().count("/openapi/v1/tmc/dict/airports") == 1


def test_an_empty_airport_answer_is_not_cached(monkeypatch):
    fake = _install(
        monkeypatch,
        {("GET", "/openapi/v1/tmc/dict/airports"): (200, {"code": "0", "data": []})},
    )

    tmc_client.airport_rows("ZZZ")
    tmc_client.airport_rows("ZZZ")

    assert fake.paths().count("/openapi/v1/tmc/dict/airports") == 2


def test_parse_date_rejects_non_iso_input_with_an_actionable_message():
    with pytest.raises(ValueError, match="yyyy-mm-dd"):
        tmc_client.parse_date("24/12/2026", "check_in")
    assert str(tmc_client.parse_date("2026-12-24", "check_in")) == "2026-12-24"
