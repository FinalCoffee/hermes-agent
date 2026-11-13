"""Seat Unique event search and ticket availability for the travel assistant, priced in US dollars.

Nothing here calls the supplier's reservation endpoints, which are not wired (see seatunique_client). A listing
that is shown with a quote_id is what place_order can book.
"""

from __future__ import annotations

import datetime as dt
import itertools
import uuid
from decimal import Decimal
from typing import Any

from . import _common, fx, quotes, seatunique_client
from ._common import ToolSpec, clean_text, compact

NOTICE = "Prices are in US dollars; converted prices are approximate. Orders here are demonstration orders."
DEFAULT_RESULTS = 8
MAX_RESULTS = 20
MAX_LISTINGS = 8
MAX_INCLUSIONS = 6
MAX_SCAN_PAGES = 3  # 3 pages of 500 cover about 1,500 events; the sandbox has about 1,020 in June 2027 alone

# Money is exact: the supplier sends minor units, and the exponent depends on the currency.
_ZERO_DECIMAL = frozenset({"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"})
_THREE_DECIMAL = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})
_DELIVERY = {"e_ticket": "e-ticket", "mobile": "mobile", "physical": "physical"}
_LISTING_KIND = {"package": "hospitality_package", "ticket": "ticket"}

_SEARCH_DESCRIPTION = (
    "Search Seat Unique event and hospitality ticket listings (mostly UK sport and music). Returns upcoming "
    "events sorted by date, with event_id, local date and time, venue, city and performer. Call "
    "get_event_tickets with an event_id for prices and availability in US dollars; converted prices are "
    "approximate. City and country match the English catalog names, filtered after a bounded "
    "scan; if truncated is true, narrow with query, city, date_to or subcategory.")
_TICKETS_DESCRIPTION = (
    "Get one event's ticket and hospitality listings sorted by price in US dollars (converted prices are "
    "approximate): price, stock, the quantities each listing allows, inclusions and one image. With quantity, "
    "says per listing whether that count is purchasable and its total. Each listing with a quote_id can be booked "
    "with place_order for the quantities it allows. Use an event_id from search_events.")

_SEARCH_SCHEMA = {
    "name": "search_events",
    "description": _SEARCH_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Part of the event name, e.g. 'Arsenal'. Case-insensitive."},
            "category": {"type": "string", "description": "Category name: 'Sport' or 'Music'."},
            "subcategory": {"type": "string", "description": "Subcategory name, e.g. 'Football', 'Rugby Union', 'Boxing'."},
            "city": {"type": "string", "description": "City name, e.g. 'London'. Case-insensitive, part of the name is enough."},
            "country": {"type": "string", "description": "Country name as the catalog spells it in English, e.g. 'United Kingdom'. 'UK', 'Great Britain' and 'Britain' also match England, Wales, Scotland and Northern Ireland."},
            "date_from": {"type": "string", "format": "date", "description": "First local date (yyyy-mm-dd). Defaults to today; earlier dates are raised to yesterday. An event earlier today can still be listed."},
            "date_to": {"type": "string", "format": "date", "description": "Last local date (yyyy-mm-dd)."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "description": f"Events to return, default {DEFAULT_RESULTS}."},
        },
        "required": [],
    },
}

_TICKETS_SCHEMA = {
    "name": "get_event_tickets",
    "description": _TICKETS_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "event_id from search_events (UUID)."},
            "quantity": {"type": "integer", "minimum": 1, "description": "Tickets or guests the user wants. Checks each listing's sale rules and stock and gives the total."},
        },
        "required": ["event_id"],
    },
}


# ---- argument parsing -------------------------------------------------------------------------------

def _optional_text(args: dict, key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text.")
    return value.strip()[:120] or None


def _optional_date(args: dict, key: str) -> dt.date | None:
    text = _optional_text(args, key)
    if text is None:
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{key} must be a date like 2027-06-01.") from None


def _optional_int(args: dict, key: str, default: int | None, low: int, high: int | None = None) -> int | None:
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        bound = f"from {low} to {high}" if high is not None else f"of at least {low}"
        raise ValueError(f"{key} must be a whole number {bound}.")
    return value


def _event_id(value: Any) -> str:
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else ""
    except ValueError:
        return ""


def _required_event_id(args: dict) -> str:
    event_id = _event_id(args.get("event_id"))
    if not event_id:
        raise ValueError("event_id is required: pass the event_id returned by search_events (a UUID).")
    return event_id


# ---- event shaping ----------------------------------------------------------------------------------

def _starts(event: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(event["date_and_time"])


def _to_float(value: Any) -> float | None:
    return float(value) if value not in (None, "") else None


def _prose(value: Any, limit: int) -> str:
    """Supplier HTML that is only punctuation (e.g. ``<p>.</p>``) carries no information, so it is dropped."""
    text = clean_text(value, limit)
    return text if any(char.isalnum() for char in text) else ""


def _event_view(event: dict) -> dict:
    starts = _starts(event)
    location = event.get("location") or {}
    performer = event.get("performer") or {}
    return compact({
        "event_id": event["id"],
        "name": clean_text(event.get("name"), 200),
        "date": starts.date().isoformat(),
        "weekday": starts.strftime("%A"),
        "time": starts.strftime("%H:%M"),
        "venue": clean_text(location.get("name"), 120),
        "city": clean_text(location.get("city"), 80),
        "country": clean_text(location.get("country"), 80),
        "category": performer.get("category"),
        "subcategory": performer.get("subcategory"),
        "performer": clean_text(performer.get("name"), 120),
        "latitude": _to_float(location.get("latitude")),
        "longitude": _to_float(location.get("longitude")),
        "description": _prose(event.get("description"), 160),
    })


# The catalog names the UK's constituent countries separately; each of these names means all five.
_UK_CATALOG_NAMES = frozenset({"united kingdom", "england", "wales", "scotland", "northern ireland"})
_COUNTRY_ALIASES = {name: _UK_CATALOG_NAMES for name in ("uk", "u.k.", "great britain", "britain", "united kingdom")}


def _country_matches(wanted: str, actual: str) -> bool:
    names = _COUNTRY_ALIASES.get(wanted.casefold())
    if names is not None:
        return actual.casefold() in names
    return wanted.casefold() in actual.casefold()


def _in_scope(event: dict, date_from: dt.date, date_to: dt.date | None, city: str | None, country: str | None) -> bool:
    local_date = _starts(event).date()
    if local_date < date_from or (date_to is not None and local_date > date_to):
        return False
    location = event.get("location") or {}
    if city and city.casefold() not in (location.get("city") or "").casefold():
        return False
    return not country or _country_matches(country, location.get("country") or "")


# ---- money and selling rules (port of the supplier client's selling-options rules) -----------------

def _exponent(currency: str) -> int:
    if currency in _ZERO_DECIMAL:
        return 0
    return 3 if currency in _THREE_DECIMAL else 2


def _source_amount(minor: int, currency: str) -> Decimal:
    """The supplier's amount in its own currency, exact: minor units scaled by the currency's exponent."""
    return Decimal(minor).scaleb(-_exponent(currency))


def _usd_key(listing: dict) -> Decimal:
    return fx.usd_value(fx.display(_source_amount(listing["price"], listing["currency"]), listing["currency"]))


def _multiple_problem(option: dict, quantity: int, available: int) -> str | None:
    step = option.get("multiplier") or option.get("selling_quantity") or 0
    if step <= 0:
        return "multiple: no step is set for this listing"
    return None if quantity % step == 0 else f"multiple: quantity must be a multiple of {step}"


def _avoid_problem(option: dict, quantity: int, available: int) -> str | None:
    remaining = available - quantity
    return f"avoid_one_or_three: would leave {remaining} unsold" if remaining in (1, 3) else None


def _no_problem(option: dict, quantity: int, available: int) -> str | None:
    return None


_RULE_PROBLEMS = {
    "any": _no_problem,
    "range": _no_problem,
    "multiple": _multiple_problem,
    "avoid_one_or_three": _avoid_problem,
}


def _problem(listing: dict, quantity: int) -> str | None:
    """Why *quantity* cannot be bought from *listing*, or None when it can."""
    option = listing["selling_option"]
    name = option["name"]
    available = listing["available_qty"]
    low, high = option["minimum_selling_quantity"], option["maximum_selling_quantity"]
    if available < 1:
        return "sold out"
    if name == "altogether":
        return None if quantity == available else f"altogether: must buy all {available} remaining tickets"
    if low > high:
        return f"{name}: minimum {low} is above maximum {high}"
    if quantity < low:
        return f"{name}: at least {low} per booking"
    if quantity > high:
        return f"{name}: at most {high} per booking"
    if name == "box":
        return None
    if quantity > available:
        return f"only {available} available"
    rule = _RULE_PROBLEMS.get(name)
    return f"{name}: selling rule is not supported" if rule is None else rule(option, quantity, available)


def _allowed(listing: dict) -> list[int]:
    option = listing["selling_option"]
    available = listing["available_qty"]
    upper = {"altogether": available, "box": option["maximum_selling_quantity"]}.get(
        option["name"], min(option["maximum_selling_quantity"], available))
    return [quantity for quantity in range(1, upper + 1) if _problem(listing, quantity) is None]


def _numbers_text(numbers: list[int]) -> str:
    if len(numbers) == 1:
        return f"exactly {numbers[0]}"
    steps = {after - before for before, after in itertools.pairwise(numbers)}
    if len(steps) == 1:
        step = steps.pop()
        span = f"{numbers[0]} to {numbers[-1]}"
        return span if step == 1 else f"{span} in steps of {step}"
    runs: list[list[int]] = []
    for number in numbers:
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return ", ".join(str(first) if first == last else f"{first} to {last}" for first, last in runs)


_RULE_SUFFIX = {
    "altogether": " (all remaining tickets together)",
    "avoid_one_or_three": " (not counts that would leave exactly 1 or 3 unsold)",
    "box": " guests, one box per booking, price per person",
}


def _describe_quantities(listing: dict) -> str:
    numbers = _allowed(listing)
    if not numbers:
        return "none at the moment"
    return _numbers_text(numbers) + _RULE_SUFFIX.get(listing["selling_option"]["name"], "")


def _quantity_view(listing: dict, quantity: int) -> dict:
    problem = _problem(listing, quantity)
    if problem is not None:
        return {"quantity": quantity, "purchasable": False, "reason": problem}
    total = _source_amount(listing["price"] * quantity, listing["currency"])
    shown = fx.display(total, listing["currency"])
    return {"quantity": quantity, "purchasable": "unconverted" not in shown, "total": shown}


def _offset_text(moment: dt.datetime) -> str:
    minutes = int(moment.utcoffset().total_seconds()) // 60
    return f"UTC{'+' if minutes >= 0 else '-'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _listing_facts(event: dict, listing: dict, allowed: list[int]) -> quotes.EventListing:
    starts = _starts(event)
    location = event.get("location") or {}
    performer = event.get("performer") or {}
    return quotes.EventListing(
        event_id=event["id"],
        event_name=clean_text(event.get("name"), 200),
        venue=clean_text(location.get("name"), 120) or None,
        category=performer.get("category") or None,
        starts_local=starts.replace(tzinfo=None),
        tz_name=_offset_text(starts),
        listing_id=listing["id"],
        listing_name=clean_text(listing.get("name"), 160),
        unit_amount=_source_amount(listing["price"], listing["currency"]),
        currency=listing["currency"],
        allowed=tuple(allowed),
    )


def _listing_view(listing: dict, quantity: int | None, event: dict) -> dict:
    unit = _source_amount(listing["price"], listing["currency"])
    view = compact({
        "listing_id": listing["id"],
        "name": clean_text(listing.get("name"), 160),
        "type": _LISTING_KIND.get(listing["listing_type"], listing["listing_type"]),
        "delivery": _DELIVERY.get(listing["ticket_type"], listing["ticket_type"]),
        "price": fx.display(unit, listing["currency"]),
        "available_qty": listing["available_qty"],
        "quantities": _describe_quantities(listing),
        "inclusions": [clean_text(item.get("name"), 120) for item in sorted(
            listing.get("inclusions") or [], key=lambda item: item.get("position", 0))[:MAX_INCLUSIONS]],
        "description": _prose(listing.get("description"), 200),
        "image_url": next((image["url"] for image in listing.get("images") or []
                           if image.get("url", "").startswith("https://")), None),
    })
    allowed = _allowed(listing)
    if allowed and fx.to_usd(unit, listing["currency"]) is not None:
        view["quote_id"] = quotes.remember(quotes.EVENT_TICKET, _listing_facts(event, listing, allowed))
    if quantity is not None:
        view["for_quantity"] = _quantity_view(listing, quantity)
    return view


# ---- handlers ---------------------------------------------------------------------------------------

@_common.tool_handler
def search_events(args: dict) -> str:
    today = dt.date.today()
    # The server's date can be a day ahead of the user's, so yesterday is the earliest date_from accepted.
    # Events earlier today may be listed: the supplier filters by date, not by the current time.
    date_from = max(_optional_date(args, "date_from") or today, today - dt.timedelta(days=1))
    date_to = _optional_date(args, "date_to")
    if date_to is not None and date_to < date_from:
        raise ValueError(f"date_to must be on or after {date_from.isoformat()}.")
    max_results = _optional_int(args, "max_results", DEFAULT_RESULTS, 1, MAX_RESULTS)
    city = _optional_text(args, "city")
    country = _optional_text(args, "country")
    category = _optional_text(args, "category")
    subcategory = _optional_text(args, "subcategory")

    filters = {"date_and_time_from": date_from.isoformat()}
    if date_to is not None:
        # A bare date is read as start of day, which would drop the events on date_to. The bound is the latest
        # instant of date_to in the westernmost venue offset (UTC-12, which is 11:59 UTC the next day); _in_scope
        # trims to the exact local date.
        filters["date_and_time_to"] = (date_to + dt.timedelta(days=1)).isoformat() + "T12:00:00+00:00"
    if query := _optional_text(args, "query"):
        filters["event_name"] = query
    if category:
        filters["category_id"] = seatunique_client.category_id(category)
    if subcategory:
        filters["subcategory_id"] = seatunique_client.subcategory_id(subcategory, filters.get("category_id"))

    scan = seatunique_client.scan_events(filters, max_pages=MAX_SCAN_PAGES)
    matched = sorted(
        (event for event in scan.events if _in_scope(event, date_from, date_to, city, country)), key=_starts)
    shown = matched[:max_results]

    hints = ["Pass an event's event_id to get_event_tickets for prices and availability."]
    if scan.truncated:
        hints.append("The scan stopped at its page budget, so later matches may be missing. Narrow with query, city, date_to or subcategory.")
    if not matched:
        hints.append("No events matched. Widen the dates, drop a filter, or check the spelling of query.")
    return _common.ok(
        notice=NOTICE,
        matched=len(matched),
        returned=len(shown),
        truncated=scan.truncated,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat() if date_to else None,
        events=[_event_view(event) for event in shown],
        hints=hints,
        note=_common.UNTRUSTED_NOTE,
    )


@_common.tool_handler
def get_event_tickets(args: dict) -> str:
    event_id = _required_event_id(args)
    quantity = _optional_int(args, "quantity", None, 1)
    event = seatunique_client.get_event(event_id)
    listings = seatunique_client.list_listings(event_id)
    sellable = sorted((listing for listing in listings if listing["available_qty"] >= 1), key=_usd_key)
    shown = sellable[:MAX_LISTINGS]

    hints = ["Prices are availability at this moment, in US dollars, and can change; converted prices are "
             "approximate. Only listings with a quote_id can be booked with place_order."]
    if quantity is not None:
        hints.append("Listings with purchasable false cannot take this quantity; offer another quantity or listing.")
    if len(sellable) > len(shown):
        hints.append(f"Showing the {len(shown)} cheapest of {len(sellable)} sellable listings.")
    return _common.ok(
        notice=NOTICE,
        event=_event_view(event),
        listings_total=len(listings),
        sold_out_listings=len(listings) - len(sellable),
        listings=[_listing_view(listing, quantity, event) for listing in shown],
        quantity=quantity,
        hints=hints,
        note=_common.UNTRUSTED_NOTE,
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(name="search_events", schema=_SEARCH_SCHEMA, handler=search_events,
             requires_env=(seatunique_client.TOKEN_ENV,)),
    ToolSpec(name="get_event_tickets", schema=_TICKETS_SCHEMA, handler=get_event_tickets,
             requires_env=(seatunique_client.TOKEN_ENV,)),
)
