"""Flight search, airport lookup and live fare pricing on the TMC open platform, in US dollars.

Nothing here calls an order endpoint. A fare that search returns is remembered as a quote (see quotes.py), which is
what place_order can book.

Round trips are not offered. TMC's round-trip search (tripType 2) returns one-leg offers, and pairing the legs
needs a staged journeyId protocol whose final step is not proven on this account. Callers search each direction.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from . import _common, fx, quotes, tmc_client
from ._common import ApiError, ToolSpec, clean_text

_TMC_ENV = (tmc_client.APP_KEY_ENV, tmc_client.APP_SECRET_ENV)
_TYPE_AIRPORT = 2
_TYPE_CITY = 1
_IATA_RE = re.compile(r"[A-Za-z]{3}")
_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?")
_CARRIER_RE = re.compile(r"[A-Z0-9]{2,3}")
_FLIGHT_NO_RE = re.compile(r"[A-Z0-9]{2,8}")
_AIRPORT_RE = re.compile(r"[A-Z]{3}")
_CABIN_MAX = 20
# Verify answers for a fare the supplier no longer sells at that price.
_STALE_OFFER_CODES = frozenset({"verify_price_failed", "verify_price_expired"})
_SORTS = ("price", "duration", "departure")
_MAX_ADULTS = 9
_MAX_RULES = 6
_NOTE = _common.UNTRUSTED_NOTE


def _iata_arg(args: dict, name: str) -> str:
    value = str(args.get(name) or "").strip().upper()
    if not _IATA_RE.fullmatch(value):
        raise ValueError(
            f"{name} must be a 3-letter IATA airport or city code, for example HKG. "
            "Call search_airports with the place name to find it."
        )
    return value


def _int_arg(args: dict, name: str, default: int, low: int, high: int) -> int:
    raw = args.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw:
        raise ValueError(f"{name} must be a whole number from {low} to {high}.")
    if not low <= int(raw) <= high:
        raise ValueError(f"{name} must be from {low} to {high}.")
    return int(raw)


def _depart_date(args: dict) -> str:
    depart = tmc_client.parse_date(args.get("depart_date"), "depart_date")
    # The server's date can be a day ahead of the user's, so yesterday is allowed and TMC has the final say.
    if depart < date.today() - timedelta(days=1):
        raise ValueError("depart_date is more than a day in the past. Use today or a later date.")
    return depart.isoformat()


def _fee(amount: Any, currency: Any) -> dict | None:
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    return fx.display(amount, str(currency or ""))


def _local(day: Any, clock: Any) -> str | None:
    return f"{day} {clock}" if day and clock else None


def _minutes(duration: str) -> int:
    match = _DURATION_RE.fullmatch(duration or "")
    if not match:
        return 0
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


def _as_list(value: Any) -> list[dict]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _resolve_code(code: str, side: str) -> tuple[int, str | None]:
    """(originType or destinationType, country) for an IATA code. Airport beats city, as the supplier's
    own search does: an airport code sent as a city is refused, while a city code also covers its airports."""
    airport_type: tuple[int, str | None] | None = None
    city_type: tuple[int, str | None] | None = None
    for row in tmc_client.airport_rows(code):
        if str(row.get("cityCode") or "").upper() == code:
            city_type = (_TYPE_CITY, row.get("countryCode"))
        for airport in _as_list(row.get("airports")):
            if (
                str(airport.get("airportCode") or "").upper() == code
                and airport.get("isActive") != 0
            ):
                airport_type = (
                    _TYPE_AIRPORT,
                    airport.get("countryCode") or row.get("countryCode"),
                )
    found = airport_type or city_type
    if found is None:
        raise ValueError(
            f"Unknown {side} code '{code}'. Call search_airports with the place name to find the code."
        )
    return found


def _baggage(rules: Any) -> dict | None:
    rows = _as_list(rules)
    adult = [rule for rule in rows if rule.get("passengerType") == "adult"] or rows
    if not adult:
        return None
    out = {}
    for label, key in (("carry_on", "portable"), ("checked", "checkIn")):
        piece = adult[0].get(key)
        if isinstance(piece, dict) and piece.get("weight") is not None:
            out[label] = (
                f"{piece.get('number') or 1} x {piece['weight']}{piece.get('unit') or 'KG'}"
            )
    return out or None


def _segment(segments: dict, segment_id: str) -> dict:
    seg = segments.get(segment_id) if isinstance(segments.get(segment_id), dict) else {}
    return _common.compact(
        {
            "airline": clean_text(seg.get("airlineName"), 60),
            "flight": seg.get("flightNumber"),
            "from": seg.get("departureAirport"),
            "depart_local": _local(seg.get("departureDate"), seg.get("departureTime")),
            "to": seg.get("arrivalAirport"),
            "arrive_local": _local(seg.get("arrivalDate"), seg.get("arrivalTime")),
            "duration": "".join(str(seg.get("flightDuration") or "").split()) or None,
        }
    )


def _offer_row(flight: dict, offer: dict, segments: dict) -> dict:
    journey = _as_list(flight.get("journeys"))[0]
    price = fx.display(_common.supplier_amount(offer.get("totalSalePrice")), offer.get("currency") or "")
    return _common.compact(
        {
            "price": price,
            "cabin": _cabin(offer),
            "airline": offer.get("ticketingAirline"),
            "depart": {
                "airport": journey.get("origin"),
                "local": _local(journey.get("originDate"), journey.get("originTime")),
            },
            "arrive": {
                "airport": journey.get("destination"),
                "local": _local(
                    journey.get("destinationDate"), journey.get("destinationTime")
                ),
            },
            "duration": "".join(str(journey.get("journeysTime") or "").split()) or None,
            "stops": journey.get("transferCount"),
            "segments": [
                _segment(segments, sid)
                for sid in journey.get("airSegmentList") or []
                if isinstance(sid, str)
            ],
            "baggage": _baggage(offer.get("baggageRules")),
        }
    )


def _cabin(offer: dict) -> str | None:
    value = (offer.get("cabinClass") or [None])[0]
    return str(value)[:_CABIN_MAX] if value else None


def _leg(seg: Any, cabin: str | None) -> quotes.FlightLeg | None:
    """One flight as an order needs it, or None when the supplier's record is too incomplete to book."""
    if not isinstance(seg, dict):
        return None
    carrier = str(seg.get("airlineCode") or "")
    flight_no = str(seg.get("flightNumber") or "")
    origin = str(seg.get("departureAirport") or "")
    destination = str(seg.get("arrivalAirport") or "")
    if not (_CARRIER_RE.fullmatch(carrier) and _FLIGHT_NO_RE.fullmatch(flight_no)
            and _AIRPORT_RE.fullmatch(origin) and _AIRPORT_RE.fullmatch(destination) and origin != destination):
        return None
    try:
        depart = datetime.fromisoformat(f"{seg['departureDate']} {seg['departureTime']}")
        arrive = datetime.fromisoformat(f"{seg['arrivalDate']} {seg['arrivalTime']}")
    except (KeyError, TypeError, ValueError):
        return None
    return quotes.FlightLeg(carrier, flight_no, origin, destination, depart, arrive, cabin)


def _fare(flight: dict, offer: dict, segments: dict, adults: int, price_key: str) -> quotes.FlightFare | None:
    """The fare as an order needs it (its legs, party and USD total), or None when it cannot be ordered: an
    incomplete leg, or a currency without a rate."""
    journey = _as_list(flight.get("journeys"))[0]
    cabin = _cabin(offer)
    legs = []
    for sid in journey.get("airSegmentList") or []:
        leg = _leg(segments.get(sid) if isinstance(sid, str) else None, cabin)
        if leg is None:
            return None
        legs.append(leg)
    total = fx.to_usd(
        _common.supplier_amount(offer.get("totalSalePrice")), offer.get("currency") or "")
    if total is None or not legs:
        return None
    return quotes.FlightFare(legs=tuple(legs), adults=adults, total=total, price_key=price_key)


_SORT_KEYS = {
    "price": lambda row: fx.usd_value(row["price"]),
    "duration": lambda row: _minutes(row.get("duration", "")),
    "departure": lambda row: row["depart"].get("local") or "",
}


def _search_body(origin: str,
    destination: str,
    depart: str,
    adults: int,
    direct_only: bool,
) -> dict:
    origin_type, origin_country = _resolve_code(origin, "origin")
    destination_type, destination_country = _resolve_code(destination, "destination")
    # A country the supplier does not list is sent as international. Domestic routes need a listed country.
    international = 0 if origin_country and origin_country == destination_country else 1
    return {
        "cabinClass": "economy",
        "tripType": 1,
        "adultNum": adults,
        "childNum": 0,
        "infantNum": 0,
        "transferNumber": 1 if direct_only else 0,
        "journeys": [
            {
                "departureDate": depart,
                "origin": origin,
                "originType": origin_type,
                "destination": destination,
                "destinationType": destination_type,
                "isInternational": international,
            }
        ],
    }


def _search_schema() -> dict:
    return {
        "name": "search_flights",
        "description": (
            "Search one-way economy flights between two airports or cities on one date, with prices in US dollars; "
            "converted prices are approximate. Returns up to max_results offers sorted by price (default), duration "
            "or departure, each with a quote_id, price, airline, local times, stops, segments and baggage. Only an "
            "offer with a quote_id can be booked with place_order. Pass a quote_id to get_flight_offer for the live "
            "price and refund and change rules. Round trips are not supported: search each direction separately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "IATA airport or city code, e.g. HKG or TPE. Use search_airports if unsure.",
                },
                "destination": {
                    "type": "string",
                    "description": "IATA airport or city code.",
                },
                "depart_date": {
                    "type": "string",
                    "description": "Departure date, yyyy-mm-dd, today or later.",
                },
                "adults": {
                    "type": "integer",
                    "description": "Adult passengers, 1-9 (default 1).",
                },
                "direct_only": {
                    "type": "boolean",
                    "description": "Only non-stop flights (default false).",
                },
                "sort": {
                    "type": "string",
                    "enum": list(_SORTS),
                    "description": "price (default), duration or departure.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Offers to return, 1-10 (default 5).",
                },
            },
            "required": ["origin", "destination", "depart_date"],
        },
    }


@_common.tool_handler
def search_flights(args: dict) -> str:
    origin, destination = _iata_arg(args, "origin"), _iata_arg(args, "destination")
    if origin == destination:
        raise ValueError("origin and destination must differ.")
    depart = _depart_date(args)
    adults = _int_arg(args, "adults", 1, 1, _MAX_ADULTS)
    direct_only = bool(args.get("direct_only", False))
    sort = str(args.get("sort") or "price")
    if sort not in _SORTS:
        raise ValueError(f"sort must be one of: {', '.join(_SORTS)}.")
    max_results = _int_arg(args, "max_results", 5, 1, 10)

    body = _search_body(origin, destination, depart, adults, direct_only)
    data = tmc_client.flight_search(body)
    segments = data.get("segments") if isinstance(data.get("segments"), dict) else {}
    priced = []
    for flight in _as_list(data.get("flight")):
        if flight.get("policyBlocked") or not _as_list(flight.get("journeys")):
            continue
        for offer in _as_list(flight.get("offerPriceList")):
            if _common.supplier_amount(offer.get("totalSalePrice")) is None or not offer.get("priceKey"):
                continue
            if (offer.get("salesLimit") or {}).get("ageLimit"):
                continue  # an age-restricted fare cannot be sold to any traveller the model can name
            fare = _fare(flight, offer, segments, adults, offer["priceKey"])
            priced.append((_offer_row(flight, offer, segments), fare))
    priced.sort(key=lambda pair: _SORT_KEYS[sort](pair[0]))
    shown = []
    for row, fare in priced[:max_results]:
        if fare is not None:
            row["quote_id"] = quotes.remember(quotes.FLIGHT, fare)
        shown.append(row)
    hint = (
        "Only offers with a quote_id can be booked. Pass one to get_flight_offer for the live price and rules "
        "before quoting a fare. Quote ids expire after 30 minutes."
        if shown
        else "No flights are on sale for this route and date. Try nearby dates or nearby airports."
    )
    return _common.ok(
        note=_NOTE,
        route=f"{origin}-{destination}",
        depart_date=depart,
        total_offers=len(priced),
        flights=shown,
        hint=hint,
    )


def _airports_schema() -> dict:
    return {
        "name": "search_airports",
        "description": (
            "Find IATA airport and city codes by place name in any language, for example '台北' or 'Taipei'. Returns "
            "city_code and each city's airports. Use a city_code in search_flights to cover all its airports, or an "
            "airport_code for one airport. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "City, airport or code, e.g. 'Taipei' or '台北'.",
                }
            },
            "required": ["keyword"],
        },
    }


@_common.tool_handler
def search_airports(args: dict) -> str:
    keyword = str(args.get("keyword") or "").strip()
    if not keyword or len(keyword) > 60:
        raise ValueError("keyword is required and must be at most 60 characters.")
    cities = []
    for row in tmc_client.airport_rows(keyword)[:8]:
        airports = [
            _common.compact(
                {
                    "airport_code": airport.get("airportCode"),
                    "name_en": clean_text(airport.get("airportNameEn"), 80),
                    "name_zh": clean_text(airport.get("airportNameZh"), 80),
                }
            )
            for airport in _as_list(row.get("airports"))
            if airport.get("isActive") != 0
        ][:6]
        cities.append(
            _common.compact(
                {
                    "city_code": row.get("cityCode"),
                    "city_name_en": clean_text(row.get("cityNameEn"), 60),
                    "city_name_zh": clean_text(row.get("cityNameZh"), 60),
                    "country": clean_text(row.get("countryNameEn"), 60),
                    "airports": airports,
                }
            )
        )
    return _common.ok(
        note=_NOTE,
        cities=cities,
        hint=(
            "No city or airport matched. Try the English or Chinese city name."
            if not cities
            else "Use city_code in search_flights for all airports of a city, or airport_code for one airport."
        ),
    )


def _rule_lines(rules: Any, currency: Any) -> list[dict]:
    """Refund or change windows from the supplier's ruler items. ``beforeAfter`` true is before departure.
    The supplier repeats the same window once per segment, so identical lines are listed once."""
    lines: list[dict] = []
    for rule in _as_list(rules):
        if rule.get("passengerType") not in (None, "adult"):
            continue
        for item in _as_list(rule.get("rulerItemList")):
            line = _common.compact(
                {
                    "when": "before departure"
                    if item.get("beforeAfter")
                    else "after departure",
                    "fee": _fee(item.get("chargeFee"), currency),
                    "days": item.get("dayCount") or None,
                    "hours": item.get("hourCount") or None,
                }
            )
            if line not in lines:
                lines.append(line)
    return lines[:_MAX_RULES]


def _verify_journeys(journeys: Any) -> list[dict]:
    return [
        _common.compact(
            {
                "route": f"{journey.get('origin')}-{journey.get('destination')}",
                "depart_local": _local(
                    journey.get("originDate"), journey.get("originTime")
                ),
                "arrive_local": _local(
                    journey.get("destinationDate"), journey.get("destinationTime")
                ),
                "flight_numbers": journey.get("flightNumberList") or None,
            }
        )
        for journey in _as_list(journeys)
    ]


def _offer_schema() -> dict:
    return {
        "name": "get_flight_offer",
        "description": (
            "Re-price one quote_id from search_flights and return the current USD total, baggage allowance, refund and "
            "change rules. Call it before quoting a fare to the user: prices can change, and quote ids expire after "
            "30 minutes. Converted prices are approximate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "quote_id": {
                    "type": "string",
                    "description": "quote_id from search_flights, e.g. Q1A2B3C4D.",
                }
            },
            "required": ["quote_id"],
        },
    }


@_common.tool_handler
def get_flight_offer(args: dict) -> str:
    quote_id = quotes.parse_id(args.get("quote_id"))
    fare = quotes.lookup(quote_id, quotes.FLIGHT).facts
    try:
        data = tmc_client.flight_verify(fare.price_key)
    except ApiError as exc:
        if exc.code in _STALE_OFFER_CODES:
            raise ApiError(
                "quote_unknown",
                "That fare is no longer sold at that price. Search flights again.",
            ) from exc
        raise
    offer = data.get("offerData") if isinstance(data.get("offerData"), dict) else {}
    currency = str(offer.get("currency") or "")
    source = _common.supplier_amount(offer.get("totalSalePrice"))
    if source is None:
        raise ApiError("quote_unknown", "That fare has no price now. Search flights again.")
    price = fx.display(source, currency)
    verified = fx.to_usd(source, currency)
    changed = verified is None or verified.amount != fare.total.amount
    if changed:
        # An order pays the price the user was last shown; a price with no rate cannot be ordered at all.
        quotes.reprice(quote_id, None if verified is None else dataclasses.replace(fare, total=verified))
    result = _common.compact(
        {
            "quote_id": quote_id,
            "price": price,
            "searched_price": fare.total.as_money() if changed else None,
            "journeys": _verify_journeys(data.get("journeys")),
            "baggage": _baggage(offer.get("baggageRules")),
            "refund_rules": _rule_lines(offer.get("refundRules"), currency),
            "change_rules": _rule_lines(offer.get("changeRules"), currency),
            "booking_notice": clean_text(data.get("bookingNotice"), 600),
        }
    )
    return _common.ok(
        note=_NOTE,
        **result,
        hint=(
            "The price changed since search: quote the current price, which is what an order now costs."
            if changed
            else "Prices and rules are live from the supplier. Converted prices are approximate."
        ),
    )


TOOLS = (
    ToolSpec(
        name="search_airports",
        schema=_airports_schema(),
        handler=search_airports,
        requires_env=_TMC_ENV,
        emoji="🛫",
    ),
    ToolSpec(
        name="search_flights",
        schema=_search_schema(),
        handler=search_flights,
        requires_env=_TMC_ENV,
        emoji="✈️",
    ),
    ToolSpec(
        name="get_flight_offer",
        schema=_offer_schema(),
        handler=get_flight_offer,
        requires_env=_TMC_ENV,
        emoji="✈️",
    ),
)
