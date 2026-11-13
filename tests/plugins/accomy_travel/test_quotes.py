"""The quote table: bound to the caller, short-lived, capped per caller and overall, and safe across threads."""

import threading

import pytest

from plugins.accomy_travel import _common, quotes
from plugins.accomy_travel._common import ApiError


@pytest.fixture(autouse=True)
def _clean_table(monkeypatch):
    quotes._quotes.clear()
    monkeypatch.setattr(_common, "caller_id", lambda: "user-a")
    yield
    quotes._quotes.clear()


def _as(monkeypatch, caller: str) -> None:
    monkeypatch.setattr(_common, "caller_id", lambda: caller)


def test_a_quote_is_found_by_the_caller_who_was_shown_it_and_its_kind(monkeypatch):
    quote_id = quotes.remember(quotes.HOTEL, "facts")

    assert quotes.lookup(quote_id, quotes.HOTEL).facts == "facts"
    assert quotes.lookup(quote_id).facts == "facts"  # any kind
    with pytest.raises(ApiError) as wrong_kind:
        quotes.lookup(quote_id, quotes.FLIGHT)
    assert wrong_kind.value.code == "quote_unknown"


def test_another_callers_quote_reads_exactly_like_an_unknown_one(monkeypatch):
    quote_id = quotes.remember(quotes.HOTEL, "facts")
    _as(monkeypatch, "user-b")

    with pytest.raises(ApiError) as foreign:
        quotes.lookup(quote_id)
    with pytest.raises(ApiError) as unknown:
        quotes.lookup("Q00000000")
    assert (foreign.value.code, foreign.value.message) == (unknown.value.code, unknown.value.message)


def test_a_quote_is_not_visible_to_the_same_caller_in_another_profile(monkeypatch):
    monkeypatch.setattr(quotes, "current_secret_scope_home", lambda: "/profiles/alpha")
    quote_id = quotes.remember(quotes.HOTEL, "facts")

    monkeypatch.setattr(quotes, "current_secret_scope_home", lambda: "/profiles/beta")
    with pytest.raises(ApiError):
        quotes.lookup(quote_id)
    monkeypatch.setattr(quotes, "current_secret_scope_home", lambda: "/profiles/alpha")
    assert quotes.lookup(quote_id).facts == "facts"


def test_a_quote_expires_after_thirty_minutes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(quotes.time, "monotonic", lambda: clock[0])
    quote_id = quotes.remember(quotes.HOTEL, "facts")

    clock[0] = 1000.0 + 29 * 60
    assert quotes.lookup(quote_id).facts == "facts"
    clock[0] = 1000.0 + 31 * 60
    with pytest.raises(ApiError):
        quotes.lookup(quote_id)


@pytest.mark.parametrize("text, expected", [("q1a2b3c4d", "Q1A2B3C4D"), (" Q1A2B3C4D ", "Q1A2B3C4D")])
def test_a_quote_id_is_read_without_case_or_spaces(text, expected):
    assert quotes.parse_id(text) == expected


@pytest.mark.parametrize("text", ["", "F1A2B3C4D", "Q1A2B3C4", "Q1A2B3C4DX", None])
def test_anything_else_is_not_a_quote_id(text):
    with pytest.raises(ValueError):
        quotes.parse_id(text)


def test_one_caller_cannot_keep_more_than_its_own_cap(monkeypatch):
    first = quotes.remember(quotes.HOTEL, "first")
    for _ in range(quotes.PER_CALLER_MAX):
        quotes.remember(quotes.HOTEL, "later")

    assert first not in quotes._quotes
    assert sum(1 for q in quotes._quotes.values() if q.owner[1] == "user-a") == quotes.PER_CALLER_MAX


def test_a_flood_evicts_the_flooders_oldest_quotes_and_never_another_callers(monkeypatch):
    monkeypatch.setattr(quotes, "TOTAL_MAX", 10)
    _as(monkeypatch, "user-b")
    bobs = [quotes.remember(quotes.HOTEL, f"b{i}") for i in range(3)]
    _as(monkeypatch, "user-a")
    for i in range(12):
        quotes.remember(quotes.HOTEL, f"a{i}")

    assert len(quotes._quotes) == 10
    assert all(quote_id in quotes._quotes for quote_id in bobs)


def test_threads_that_remember_at_once_never_break_the_table_or_its_cap(monkeypatch):
    monkeypatch.setattr(quotes, "TOTAL_MAX", 50)
    errors = []

    def worker():
        try:
            for _ in range(100):
                quotes.remember(quotes.HOTEL, "facts")
        except Exception as exc:  # reported below, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(quotes._quotes) == quotes.PER_CALLER_MAX
