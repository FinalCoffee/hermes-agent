"""Seat Unique Trade API reads: events, listings and the category catalog.

Read-only by construction: no reservation or payment endpoint is wired here, so nothing reachable
from the travel tools can create, pay for or cancel an order. Sandbox only, because live access is
owner-gated.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from . import _common

BASE_URL = "https://api.sandbox.dev.seatunique.tech/trade_api"
TOKEN_ENV = "SEATUNIQUE_API_TOKEN"
# The supplier's maximum page size: fewest calls per scan.
SCAN_PAGE_SIZE = 500
# Categories and subcategories change rarely; the cache holds one entry per category, a handful in total.
CATALOG_TTL_SECONDS = 3600.0

# Seat Unique allows 120 requests a minute per API key, and every chat shares that key. The budget keeps 20 in
# reserve. Per-user limits in _common do not bound this, because one user's search fans out into several requests.
REQUESTS_PER_MINUTE = 100

_now: Callable[[], float] = time.monotonic
_cache: dict[tuple[str, ...], tuple[float, Any]] = {}
_budget = _common.RateLimiter()

# Supplier status to a stable code and a model-facing sentence. Supplier text is never forwarded:
# its error bodies are not needed to act, and they can echo request details.
_STATUS_ERRORS: dict[int, tuple[str, str]] = {
    401: ("auth", "The event ticket service is not accepting this assistant's access, so event tickets "
                  "are unavailable right now. Tell the user and offer other travel help."),
    403: ("auth", "The event ticket service is not accepting this assistant's access, so event tickets "
                  "are unavailable right now. Tell the user and offer other travel help."),
    404: ("not_found", "No such event was found. Use search_events to get a current event_id."),
    400: ("invalid_request", "The event ticket service rejected the request. Try different search terms."),
    422: ("invalid_request", "The event ticket service rejected the request. Try different search terms."),
    429: ("rate_limited", "The event ticket service is busy. Try again shortly."),
}
_UNAVAILABLE = ("unavailable", "The event ticket service is unavailable right now. Try again shortly.")
_BAD_SHAPE = ("bad_response", "The event ticket service returned an unexpected answer. Try again shortly.")


@dataclass(frozen=True)
class ScanResult:
    events: list[dict]
    truncated: bool


def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    """GET one endpoint and return its whole ``{data, metadata}`` body."""
    if not _budget.allow("seatunique", REQUESTS_PER_MINUTE, 60.0):
        raise _common.ApiError(*_STATUS_ERRORS[429])
    headers = {"Authorization": f"Bearer {_common.secret(TOKEN_ENV)}"}
    status, body = _common.request_json("GET", BASE_URL + path, headers=headers, params=params)
    if status != 200:
        code, message = _STATUS_ERRORS.get(status, _UNAVAILABLE)
        raise _common.ApiError(code, message, status=status)
    if not isinstance(body, dict) or "data" not in body:
        raise _common.ApiError(*_BAD_SHAPE)
    return body


def _cached(key: tuple[str, ...], load: Callable[[], Any]) -> Any:
    stamp = _cache.get(key)
    if stamp is not None and _now() - stamp[0] < CATALOG_TTL_SECONDS:
        return stamp[1]
    value = load()
    _cache[key] = (_now(), value)
    return value


def _categories() -> list[dict]:
    return _cached(("categories",), lambda: _get("/v1/categories")["data"])


def _subcategories(category_id: str) -> list[dict]:
    return _cached(
        ("subcategories", category_id),
        lambda: _get(f"/v1/categories/{category_id}/subcategories")["data"])


def _find_by_name(items: list[dict], name: str) -> str | None:
    wanted = name.casefold()
    for item in items:
        if item["name"].casefold() == wanted:
            return item["id"]
    return None


def category_id(name: str) -> str:
    found = _find_by_name(_categories(), name)
    if found is None:
        known = ", ".join(sorted(item["name"] for item in _categories()))
        raise ValueError(f"Unknown category '{name}'. Use one of: {known}.")
    return found


def subcategory_id(name: str, category: str | None) -> str:
    """Subcategory id, searched under *category* when given and under every category otherwise."""
    owners = [category] if category else [item["id"] for item in _categories()]
    known: set[str] = set()
    for owner in owners:
        subcategories = _subcategories(owner)
        found = _find_by_name(subcategories, name)
        if found is not None:
            return found
        known.update(item["name"] for item in subcategories)
    raise ValueError(f"Unknown subcategory '{name}'. Known subcategories: {', '.join(sorted(known))}.")


def scan_events(filters: dict[str, str], *, max_pages: int) -> ScanResult:
    """Walk the event catalog page by page, deduplicated by id, for at most *max_pages* pages.

    Offset paging can skip or repeat rows when the catalog changes mid-scan, hence the dedupe.
    ``truncated`` means matching events may exist beyond the page budget.
    """
    seen: dict[str, dict] = {}
    page = 1
    while True:
        body = _get("/v1/events", {**filters, "per_page": SCAN_PAGE_SIZE, "page": page})
        for event in body["data"]:
            seen.setdefault(event["id"], event)
        next_page = (body.get("metadata") or {}).get("next_page")
        if not body["data"] or next_page is None:
            return ScanResult(list(seen.values()), truncated=False)
        if page >= max_pages:
            return ScanResult(list(seen.values()), truncated=True)
        if not isinstance(next_page, int) or next_page <= page:
            raise _common.ApiError(*_BAD_SHAPE)
        page = next_page


def get_event(event_id: str) -> dict:
    return _get(f"/v1/events/{event_id}")["data"]


def list_listings(event_id: str) -> list[dict]:
    """Every listing of one event; the supplier does not paginate this endpoint."""
    return _get(f"/v1/events/{event_id}/listings")["data"]
