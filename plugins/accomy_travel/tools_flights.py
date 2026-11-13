"""Flight search, airport lookup and live fare pricing on the TMC open platform, in US dollars.

Nothing here calls an order endpoint. A fare that search returns is remembered as a quote (see quotes.py), which is
what place_order can book. Search shows only fares within the travel standard (standard.py), so only those get one.

A journey out and back is searched as one round trip, and one that comes home from another city as one multi-city
trip: on full-service routes the supplier sells either for far less than the one-way fares added up (Hong Kong to
London and back was 765 US dollars against 893 in a live probe). TMC answers such a search one leg at a time:

* the search (tripType 2 or 3, every leg in ``journeys``) returns the flights of leg 1, each with the lowest price
  of the whole trip that uses it and no price key;
* the same search with ``journeyId`` listing the flights picked so far returns the flights of the next leg;
* only the last leg's flights carry a price key, and verify refuses it: the fares of the finished itinerary are
  read first (tmc_client.flight_fares), and one of those verifies.

search_flights follows that: a flight of an earlier leg gets an option_id (quotes.FLIGHT_CHOICE), which the next
call takes as continue_from, and a flight of the last leg gets the quote for the whole trip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from . import _common, fx, quotes, standard, tmc_client
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
# The supplier's cabin names, without case, spaces, hyphens and underscores ("premium_economy", "PremiumEconomy").
_CABIN_SEPARATORS_RE = re.compile(r"[\s_-]")
_CABIN_BY_KEY = {"economy": "economy", "premiumeconomy": "premium_economy", "business": "business"}
# Verify answers for a fare the supplier no longer sells at that price.
_STALE_OFFER_CODES = frozenset({"verify_price_failed", "verify_price_expired"})
_SORTS = ("price", "duration", "departure")
_MAX_ADULTS = 9
# Legs of one trip: a round trip has two, a multi-city trip up to this many.
MAX_LEGS = 4
_TRIP_TYPES = {quotes.ONE_WAY: 1, quotes.ROUND_TRIP: 2, quotes.MULTI_CITY: 3}
_TRIP_KINDS = {trip_type: kind for kind, trip_type in _TRIP_TYPES.items()}
_TRIP_NAMES = {quotes.ROUND_TRIP: "round trip", quotes.MULTI_CITY: "multi-city trip"}
# Fares of one itinerary that get_flight_offer asks the supplier to price, cheapest first, before it gives up.
_FARES_TRIED = 3
# What a search is made of. A later leg (continue_from) keeps all of it as first searched.
_SEARCH_ARGS = ("origin", "destination", "depart_date", "return_date", "more_legs", "adults", "cabin", "direct_only")
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


def _later_date(value: Any, name: str, previous: str) -> str:
    """The date of a later leg, which cannot be before the leg it follows."""
    day = tmc_client.parse_date(value, name).isoformat()
    if day < previous:
        raise ValueError(f"{name} is before {previous}, the date of the leg it follows.")
    return day


def _route(args: dict) -> tuple[str, str]:
    origin, destination = _iata_arg(args, "origin"), _iata_arg(args, "destination")
    if origin == destination:
        raise ValueError("origin and destination must differ.")
    return origin, destination


def _legs_asked(args: dict) -> list[tuple[str, str, str]]:
    """The trip as (origin, destination, date) per leg: one for a one-way, two for a round trip, and as many as
    were listed for a multi-city trip."""
    legs = [(*_route(args), _depart_date(args))]
    back, more = args.get("return_date"), args.get("more_legs")
    if back and more:
        raise ValueError("Pass return_date for a round trip or more_legs for a multi-city trip, not both.")
    if back:
        origin, destination, depart = legs[0]
        legs.append((destination, origin, _later_date(back, "return_date", depart)))
    if more is not None:
        if (not isinstance(more, list) or not 1 <= len(more) < MAX_LEGS
                or not all(isinstance(leg, dict) for leg in more)):
            raise ValueError(
                f"more_legs lists 1 to {MAX_LEGS - 1} further legs, each with origin, destination and depart_date.")
        for leg in more:
            legs.append((*_route(leg), _later_date(leg.get("depart_date"), "depart_date of a later leg", legs[-1][2])))
    return legs


def _trip_kind(legs: list[tuple[str, str, str]]) -> str:
    if len(legs) == 1:
        return quotes.ONE_WAY
    # Two legs that mirror each other are a round trip however they were written: the supplier prices it as one.
    if len(legs) == 2 and legs[1][:2] == (legs[0][1], legs[0][0]):
        return quotes.ROUND_TRIP
    return quotes.MULTI_CITY


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


def _flying_minutes(journey: dict, segments: dict) -> int | None:
    """Time in the air: the flights' own durations added up, so the time on the ground at a stop is not counted.
    None when the supplier gives no duration for one of the flights."""
    total = 0
    for sid in journey.get("airSegmentList") or []:
        seg = segments.get(sid) if isinstance(sid, str) else None
        minutes = _minutes("".join(str((seg or {}).get("flightDuration") or "").split()))
        if not minutes:
            return None
        total += minutes
    return total or None


def _hours_minutes(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return f"{hours}h{rest}m" if rest else f"{hours}h"


def _cabins_allowed(offer: dict, searched: str, flown: list[int | None]) -> bool:
    """Whether the offer puts every flight in a cabin the travel standard allows for the flying time of its leg.

    The supplier names one cabin per flight of the whole trip, in travel order. *flown* holds, in the same order,
    the flying time of the leg each flight belongs to, as far as the legs are known: on an earlier leg of a trip
    the flights of the legs not yet picked are judged when their leg is. A name outside the standard's cabins
    (first class) is kept as it is, so the standard refuses it."""
    named = [str(value) for value in offer.get("cabinClass") or [] if value]
    cabins = [_CABIN_BY_KEY.get(_CABIN_SEPARATORS_RE.sub("", name.lower()), name) for name in named]
    if len(cabins) < len(flown):
        # No cabin for some flight, or none at all: hold what is named, or the searched cabin, against the
        # shortest leg.
        shortest = None if None in flown else min(flown)
        return all(standard.cabin_allowed(cabin, shortest) for cabin in cabins or [searched])
    return all(standard.cabin_allowed(cabin, minutes) for cabin, minutes in zip(cabins, flown))


def _leg_times(legs: tuple[quotes.FlightLeg, ...], flying: tuple[int | None, ...]) -> list[int | None]:
    """For each flight in *legs*, the flying time of the leg of the trip it belongs to."""
    return [flying[leg.journey - 1] for leg in legs]


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


def _allowance(piece: Any) -> str | None:
    """One baggage allowance in words, or None when the supplier states none: it writes -1 for a count or a
    weight it does not know, and 0 for both as often for "no data" as for "nothing included"."""
    if not isinstance(piece, dict):
        return None
    number, weight = piece.get("number"), piece.get("weight")
    pieces = number if isinstance(number, int) and not isinstance(number, bool) and number > 0 else None
    kilos = weight if isinstance(weight, (int, float)) and not isinstance(weight, bool) and weight > 0 else None
    if kilos:
        weighed = f"{kilos}{piece.get('unit') or 'KG'}"
        return f"{pieces} x {weighed}" if pieces else weighed
    if pieces:
        return f"{pieces} piece" if pieces == 1 else f"{pieces} pieces"
    return None


def _baggage(rules: Any, segment_ids: Any = None) -> dict | None:
    """The adult baggage allowance of the flights in *segment_ids* (of every flight when none are given): one
    answer when they all have the same, else one per flight number, as on a trip of several airlines.

    The fare of an earlier leg of a trip is the lowest of the whole trip, so its rules also cover flights of
    legs nobody has picked yet: a row shows the allowance of its own flights only."""
    rows = _as_list(rules)
    by_flight: dict[str, dict] = {}
    for rule in [rule for rule in rows if rule.get("passengerType") == "adult"] or rows:
        segment_id = str(rule.get("segmentId") or "")
        if segment_ids is not None and segment_id not in segment_ids:
            continue
        allowance = {
            label: words for label, key in (("carry_on", "portable"), ("checked", "checkIn"))
            if (words := _allowance(rule.get(key)))}
        # The supplier names a rule's flight as date-origin-destination-flight number.
        by_flight.setdefault(segment_id.rpartition("-")[2], allowance)
    allowances = list(by_flight.values())
    if all(allowance == allowances[0] for allowance in allowances):
        return (allowances[0] if allowances else None) or None
    return {"by_flight": {flight: allowance for flight, allowance in by_flight.items() if allowance}}


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


def _offer_row(journey: dict, offer: dict, segments: dict, flying: int | None) -> dict:
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
            "flying_time": _hours_minutes(flying) if flying else None,
            "stops": journey.get("transferCount"),
            "segments": [
                _segment(segments, sid)
                for sid in journey.get("airSegmentList") or []
                if isinstance(sid, str)
            ],
            "baggage": _baggage(offer.get("baggageRules"), journey.get("airSegmentList") or []),
        }
    )


def _cabin(offer: dict) -> str | None:
    value = (offer.get("cabinClass") or [None])[0]
    return str(value)[:_CABIN_MAX] if value else None


def _leg(seg: Any, cabin: str | None, journey: int) -> quotes.FlightLeg | None:
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
    return quotes.FlightLeg(carrier, flight_no, origin, destination, depart, arrive, cabin, journey)


def _legs(journey: dict, offer: dict, segments: dict, leg_no: int, before: int) -> tuple[quotes.FlightLeg, ...] | None:
    """The flights of one leg of a trip as an order needs them, or None when one is too incomplete to book.

    The supplier names one cabin for each flight of the whole trip, in travel order, so this leg's cabins come
    after those of the *before* flights of the legs already picked."""
    cabins = [str(value)[:_CABIN_MAX] for value in offer.get("cabinClass") or [] if value]
    legs = []
    for index, sid in enumerate(journey.get("airSegmentList") or []):
        cabin = cabins[before + index] if before + index < len(cabins) else _cabin(offer)
        leg = _leg(segments.get(sid) if isinstance(sid, str) else None, cabin, leg_no)
        if leg is None:
            return None
        legs.append(leg)
    return tuple(legs) or None


_SORT_KEYS = {
    "price": lambda row: fx.usd_value(row["price"]),
    "duration": lambda row: _minutes(row.get("duration", "")),
    "departure": lambda row: row["depart"].get("local") or "",
}


def _journey(origin: str, destination: str, depart: str) -> dict:
    origin_type, origin_country = _resolve_code(origin, "origin")
    destination_type, destination_country = _resolve_code(destination, "destination")
    # A country the supplier does not list is sent as international. Domestic routes need a listed country.
    international = 0 if origin_country and origin_country == destination_country else 1
    return {
        "departureDate": depart,
        "origin": origin,
        "originType": origin_type,
        "destination": destination,
        "destinationType": destination_type,
        "isInternational": international,
    }


def _search_body(legs: list[tuple[str, str, str]], adults: int, direct_only: bool, cabin: str) -> dict:
    """The supplier request for the whole trip. It is sent as it is for leg 1 and again, with the flights picked
    so far, for each later leg."""
    return {
        "cabinClass": cabin,
        "tripType": _TRIP_TYPES[_trip_kind(legs)],
        "adultNum": adults,
        "childNum": 0,
        "infantNum": 0,
        "transferNumber": 1 if direct_only else 0,
        "journeys": [_journey(*leg) for leg in legs],
    }


def _leg_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "IATA airport or city code this leg leaves from."},
            "destination": {"type": "string", "description": "IATA airport or city code this leg arrives at."},
            "depart_date": {"type": "string", "description": "Date of this leg, yyyy-mm-dd."},
        },
        "required": ["origin", "destination", "depart_date"],
    }


def _search_schema() -> dict:
    return {
        "name": "search_flights",
        "description": (
            "Search flights, with prices in US dollars; converted prices are approximate. A journey out and back "
            "is ONE round trip: pass return_date. It costs far less than two one-way fares, so never search or "
            "order the two directions separately, even when the user names them one at a time. A trip that comes "
            "home from another city (Taipei to Tokyo, then Osaka to Kaohsiung) is ONE multi-city trip: pass "
            "more_legs. Search one way only when the user flies one way. A round trip or multi-city trip is "
            "searched leg by leg: this call returns the flights of leg 1, each with an option_id and "
            "trip_price_from, the lowest price of the whole trip with that flight. Call again with only "
            "continue_from set to the option_id of the flight chosen, for the flights of the next leg. The flights "
            "of the last leg carry a quote_id and the price of the whole trip. Returns up to max_results flights "
            "sorted by price (default), duration or departure, each with cabin, airline, local times, "
            "flying_time, stops, segments and baggage. Only offers within the travel standard are returned: "
            "outside_travel_standard counts the ones left out, and fare_limit is the dearest fare the standard "
            "allows in this search. Only an offer with a quote_id can be booked with place_order. Pass a quote_id "
            "to get_flight_offer for the live price and refund and change rules."
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
                "return_date": {
                    "type": "string",
                    "description": "Round trip: the date of the flight back from destination to origin, yyyy-mm-dd.",
                },
                "more_legs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_LEGS - 1,
                    "items": _leg_schema(),
                    "description": (
                        "Multi-city trip: the legs after the first, in travel order, for a trip that does not "
                        f"simply fly back the way it came. Up to {MAX_LEGS} legs in all."
                    ),
                },
                "continue_from": {
                    "type": "string",
                    "description": (
                        "The option_id of the flight chosen for the leg before. Pass it alone, with sort and "
                        "max_results if wanted: the trip, the passengers and the cabin stay as first searched."
                    ),
                },
                "adults": {
                    "type": "integer",
                    "description": "Adult passengers, 1-9 (default 1).",
                },
                "cabin": {
                    "type": "string",
                    "enum": list(standard.CABINS),
                    "description": (
                        "Cabin class (default economy). premium_economy and business are within the travel "
                        f"standard only when every leg has {standard.PREMIUM_CABIN_MIN_HOURS} hours of flying "
                        "time or more; search them only when the user asks for that cabin."
                    ),
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
            # A new search needs origin, destination and depart_date; a later leg needs continue_from alone.
            "required": [],
        },
    }


@dataclass(frozen=True)
class _Offer:
    """One flight of a leg at the price the supplier gives it, with what the travel standard judges it by."""

    row: dict
    legs: tuple[quotes.FlightLeg, ...] | None  # None when the supplier's record is too incomplete to book
    total: fx.Converted | None  # in US dollars; None for a currency without a rate, which the fare rule cannot judge
    flying: int | None
    in_cabin: bool  # the cabin rule of the travel standard, for this flight and the ones picked before it
    journey_id: str
    price_key: str

    @property
    def usd(self) -> Decimal | None:
        return None if self.total is None else self.total.amount


def _offers(data: dict, picked: quotes.FlightChoice) -> list[_Offer]:
    """Every flight the supplier sells for the leg that follows the flights in *picked*, before the travel
    standard is applied."""
    search = picked.search
    leg_no = len(picked.journey_ids) + 1
    last = leg_no == len(search["journeys"])
    segments = data.get("segments") if isinstance(data.get("segments"), dict) else {}
    offers = []
    for flight in _as_list(data.get("flight")):
        journeys = _as_list(flight.get("journeys"))
        if flight.get("policyBlocked") or not journeys:
            continue
        journey = journeys[0]
        journey_id, flying = str(journey.get("journeyId") or ""), _flying_minutes(journey, segments)
        flown = _leg_times(picked.legs, picked.flying) + [flying] * len(journey.get("airSegmentList") or [])
        for offer in _as_list(flight.get("offerPriceList")):
            amount, price_key = _common.supplier_amount(offer.get("totalSalePrice")), str(offer.get("priceKey") or "")
            # The trip is carried on by a price key from its last leg and by a journey id from an earlier one.
            if amount is None or not (price_key if last else journey_id):
                continue
            if (offer.get("salesLimit") or {}).get("ageLimit"):
                continue  # an age-restricted fare cannot be sold to any traveller the model can name
            legs = _legs(journey, offer, segments, leg_no, len(picked.legs))
            total = fx.to_usd(amount, offer.get("currency") or "")
            # A flight that cannot be ordered is still shown on the last leg, as a one-way always was. On an
            # earlier leg it would be a dead end: nothing could carry the trip on from it.
            if not last and (legs is None or total is None):
                continue
            offers.append(_Offer(
                row=_offer_row(journey, offer, segments, flying),
                legs=legs,
                total=total,
                flying=flying,
                in_cabin=_cabins_allowed(offer, search["cabinClass"], flown),
                journey_id=journey_id,
                price_key=price_key,
            ))
    return offers


def _within_standard(offers: list[_Offer], picked: quotes.FlightChoice) -> tuple[list[_Offer], Decimal | None]:
    """The offers the travel standard allows, and the dearest fare it allows among them: first the cabin rule
    by flying time, then the fare rule. The fare limit of a trip is set by its first leg, whose cheapest price
    is the cheapest whole trip, and the later legs keep it."""
    in_cabin = [offer for offer in offers if offer.in_cabin]
    limit = picked.fare_limit
    if not picked.journey_ids:
        prices = [offer.usd for offer in in_cabin if offer.usd is not None]
        limit = standard.fare_limit(min(prices)) if prices else None
    if limit is None:
        return in_cabin, None
    return [offer for offer in in_cabin if offer.usd is None or offer.usd <= limit], limit


def _search_hint(found: int, within: int, cabin: str, leg_no: int, leg_count: int, trip: str) -> str:
    if within:
        if leg_no < leg_count:
            hint = (
                f"These are the flights of leg {leg_no} of {leg_count} of one {trip}. trip_price_from is the "
                "lowest price of the whole trip with that flight, every leg and every passenger included: never "
                "give it as the price of this flight alone, and never add the prices of two legs together. Show "
                "the flights and ask which one the user takes; take the cheapest yourself only when they asked "
                "for the cheapest trip or left the choice to you. Then continue from that one flight: call "
                "search_flights with continue_from set to its option_id, and nothing else but sort or "
                f"max_results, for the flights of leg {leg_no + 1}. An option_id cannot be ordered, and it "
                "expires after 30 minutes."
            )
        elif leg_count > 1:
            hint = (
                f"These are the flights of the last leg. Each price is for the whole {trip}: the flights in "
                "chosen and this one, for every passenger. Show them, and pass the quote_id of the flight the "
                "user takes to get_flight_offer for the live price and rules before the order summary: the live "
                "price is often a little above the one shown here. Only offers with a quote_id can be booked. "
                "Quote ids expire after 30 minutes."
            )
        else:
            hint = (
                "Only offers with a quote_id can be booked. Pass one to get_flight_offer for the live price and "
                "rules before quoting a fare. Quote ids expire after 30 minutes."
            )
        if found > within:
            hint += (
                f" {found - within} offers outside the travel standard were left out: tell the user how many "
                "and why."
            )
        return hint
    # A later leg keeps the trip as first searched, so its way out is another flight of the leg before or a
    # new search of the whole trip: a date passed with continue_from would change nothing.
    back = (
        " Continue from another option_id of the leg before. Other dates need a new search of the whole trip, "
        "without continue_from."
    )
    if not found:
        if leg_no > 1:
            return "No flight of this leg is sold together with the flights chosen so far." + back
        return "No flights are on sale for this route and date. Try nearby dates or nearby airports."
    if cabin != standard.ECONOMY:
        return (
            f"Every {cabin} fare found is outside the travel standard: that cabin needs "
            f"{standard.PREMIUM_CABIN_MIN_HOURS} hours of flying time or more on every leg. Tell the user, and "
            + ("search the whole trip again in economy, without continue_from." if leg_no > 1 else "search economy.")
        )
    if leg_no > 1:
        return "Flights are on sale for this leg, but none is within the travel standard. Tell the user." + back
    return "Flights are on sale, but none is within the travel standard. Tell the user, and try other dates."


def _new_search(args: dict) -> quotes.FlightChoice:
    """A search as its arguments ask for it, with no flight picked yet."""
    legs = _legs_asked(args)
    adults = _int_arg(args, "adults", 1, 1, _MAX_ADULTS)
    cabin = str(args.get("cabin") or standard.ECONOMY)
    if cabin not in standard.CABINS:
        raise ValueError(
            f"cabin must be one of: {', '.join(standard.CABINS)}. First class is outside the travel standard."
        )
    search = _search_body(legs, adults, bool(args.get("direct_only", False)), cabin)
    return quotes.FlightChoice(search=search, journey_ids=(), legs=(), flying=(), fare_limit=None)


def _picked(args: dict) -> quotes.FlightChoice | None:
    """The flights the caller picked for the earlier legs, when the call continues a trip (continue_from)."""
    raw = args.get("continue_from")
    if not raw:
        return None
    changed = [name for name in _SEARCH_ARGS if args.get(name) is not None]
    if changed:
        # Dropping them in silence would return the old trip's flights as if they answered the new request.
        raise ValueError(
            f"continue_from goes alone: leave out {', '.join(changed)}. The trip, its dates, the passengers "
            "and the cabin stay as first searched. To change any of them, search the whole trip again without "
            "continue_from.")
    option_id = str(raw).strip().upper()
    if not quotes.QUOTE_ID_RE.fullmatch(option_id):
        raise ValueError("continue_from is the option_id of a flight in the results of the leg before.")
    try:
        quote = quotes.lookup(option_id)
    except ApiError as exc:
        raise ApiError(
            exc.code,
            "That option is unknown or has expired. Search the whole trip again, from its first leg.") from None
    if quote.kind != quotes.FLIGHT_CHOICE:
        raise ValueError(
            "continue_from takes an option_id, which the flights of an earlier leg carry. A quote_id is already "
            "a whole fare: pass it to get_flight_offer or place_order.")
    return quote.facts


def _chosen(legs: tuple[quotes.FlightLeg, ...]) -> list[dict]:
    """The flights picked for the earlier legs, one row per leg, so the model can name the whole trip."""
    rows = []
    for number in sorted({leg.journey for leg in legs}):
        flights = [leg for leg in legs if leg.journey == number]
        rows.append({
            "leg": number,
            "route": f"{flights[0].origin}-{flights[-1].destination}",
            "depart_local": f"{flights[0].depart_local:%Y-%m-%d %H:%M}",
            "arrive_local": f"{flights[-1].arrive_local:%Y-%m-%d %H:%M}",
            "flights": [leg.flight_no for leg in flights],
        })
    return rows


def _shown(offer: _Offer, picked: quotes.FlightChoice, limit: Decimal | None, last: bool) -> dict:
    """The offer's row as the model sees it. A flight that can be ordered gets the id that carries it on: the
    quote of the whole trip on the last leg, an option for the next leg before that."""
    search = picked.search
    row = offer.row if last else {
        ("trip_price_from" if key == "price" else key): value for key, value in offer.row.items()}
    if offer.legs is None or offer.total is None:
        return row
    legs, flying = (*picked.legs, *offer.legs), (*picked.flying, offer.flying)
    if last:
        # The limit goes with the quote: a fare that verifies above it later is outside the standard too.
        row["quote_id"] = quotes.remember(quotes.FLIGHT, quotes.FlightFare(
            legs=legs, adults=search["adultNum"], total=offer.total, price_key=offer.price_key, fare_limit=limit,
            trip_kind=_TRIP_KINDS[search["tripType"]], flying=flying))
    else:
        row["option_id"] = quotes.remember(quotes.FLIGHT_CHOICE, quotes.FlightChoice(
            search=search, journey_ids=(*picked.journey_ids, offer.journey_id), legs=legs, flying=flying,
            fare_limit=limit))
    return row


@_common.tool_handler
def search_flights(args: dict) -> str:
    sort = str(args.get("sort") or "price")
    if sort not in _SORTS:
        raise ValueError(f"sort must be one of: {', '.join(_SORTS)}.")
    max_results = _int_arg(args, "max_results", 5, 1, 10)
    picked = _picked(args) or _new_search(args)
    search = picked.search
    leg_no, leg_count = len(picked.journey_ids) + 1, len(search["journeys"])
    last, cabin = leg_no == leg_count, search["cabinClass"]

    request = {**search, "journeyId": list(picked.journey_ids)} if picked.journey_ids else search
    offers = _offers(tmc_client.flight_search(request), picked)
    within, limit = _within_standard(offers, picked)
    within.sort(key=lambda offer: _SORT_KEYS[sort](offer.row))
    journey = search["journeys"][leg_no - 1]
    result: dict[str, Any] = {"note": _NOTE}
    if leg_count > 1:
        result.update(trip=_TRIP_KINDS[search["tripType"]].lower(), leg=leg_no, legs=leg_count)
        if picked.legs:
            result["chosen"] = _chosen(picked.legs)
    result.update(
        route=f"{journey['origin']}-{journey['destination']}",
        depart_date=journey["departureDate"],
        cabin=cabin,
        total_offers=len(within),
        outside_travel_standard=len(offers) - len(within),
        flights=[_shown(offer, picked, limit, last) for offer in within[:max_results]],
        hint=_search_hint(
            len(offers), len(within), cabin, leg_no, leg_count, _TRIP_NAMES.get(_TRIP_KINDS[search["tripType"]], "")),
    )
    if limit is not None:
        result["fare_limit"] = {"amount": f"{limit:.2f}", "currency": fx.BASE}
    return _common.ok(**result)


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
            "No city or airport matched. Try the English or Chinese city name, or the nearest city that has an "
            "airport: a place such as Kyoto has none of its own and is reached through Osaka."
            if not cities
            else "Use city_code in search_flights for all airports of a city, or airport_code for one airport."
        ),
    )


def _rule_lines(rules: Any, currency: Any) -> list[dict]:
    """Refund or change windows from the supplier's ruler items. ``beforeAfter`` true is before departure.
    The supplier repeats the same window once per segment, so identical lines are listed once. It writes a
    fee of -1 where it gives no amount, and nothing it sends says whether that means not allowed or not
    known: the line says the fee is not stated instead of showing a price of minus one."""
    lines: list[dict] = []
    for rule in _as_list(rules):
        if rule.get("passengerType") not in (None, "adult"):
            continue
        for item in _as_list(rule.get("rulerItemList")):
            charge = item.get("chargeFee")
            fee = None if isinstance(charge, (int, float)) and charge < 0 else _fee(charge, currency)
            line = _common.compact(
                {
                    "when": "before departure"
                    if item.get("beforeAfter")
                    else "after departure",
                    "fee": fee or "not stated by the supplier",
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
            "Re-price one quote_id from search_flights and return the current USD total (of the whole trip, for a "
            "round trip or a multi-city trip), baggage allowance, refund and change rules. Call it before quoting a "
            "fare to the user: prices can change, and quote ids expire after 30 minutes. Converted prices are "
            "approximate."
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


def _trip_fare_keys(fare: quotes.FlightFare) -> list[str]:
    """The price keys of the fares on sale for a whole round trip or multi-city trip, cheapest first: the ones
    within the cabin rule, at most ``_FARES_TRIED`` of them."""
    data = tmc_client.flight_fares(fare.price_key)
    searched, flown = fare.legs[0].cabin or standard.ECONOMY, _leg_times(fare.legs, fare.flying)
    priced = []
    for flight in _as_list(data.get("flight")):
        for offer in _as_list(flight.get("offerPriceList")):
            amount = _common.supplier_amount(offer.get("totalSalePrice"))
            usd = None if amount is None else fx.to_usd(amount, offer.get("currency") or "")
            if usd is None or not offer.get("priceKey") or (offer.get("salesLimit") or {}).get("ageLimit"):
                continue
            if _cabins_allowed(offer, searched, flown):
                priced.append((usd.amount, str(offer["priceKey"])))
    priced.sort(key=lambda item: item[0])
    return [key for _, key in priced[:_FARES_TRIED]]


def _verified(fare: quotes.FlightFare) -> dict:
    """The supplier's live answer for the fare. A one-way is verified by the key its search gave. A longer trip
    is verified by one of the fares the supplier sells for the finished itinerary, the cheapest that still
    holds."""
    one_way = fare.trip_kind == quotes.ONE_WAY
    for key in [fare.price_key] if one_way else _trip_fare_keys(fare):
        try:
            return tmc_client.flight_verify(key)
        except ApiError as exc:
            if exc.code not in _STALE_OFFER_CODES:
                raise
    if one_way:
        raise ApiError("quote_unknown", "That fare is no longer sold at that price. Search flights again.")
    raise ApiError(
        "quote_unknown",
        "These flights are not sold together now. Offer the user another flight of the same leg: the other quote "
        "ids of that search still stand. Or search the trip again.")


@_common.tool_handler
def get_flight_offer(args: dict) -> str:
    quote_id = quotes.parse_id(args.get("quote_id"))
    quote = quotes.lookup(quote_id, quotes.FLIGHT, quotes.FLIGHT_CHOICE)
    if quote.kind == quotes.FLIGHT_CHOICE:
        raise ApiError(
            "trip_incomplete",
            "That id is a flight picked for one leg of a trip, not a fare. Call search_flights with "
            "continue_from set to it, choose the remaining legs, and use the quote_id of the last leg.")
    fare = quote.facts
    data = _verified(fare)
    offer = data.get("offerData") if isinstance(data.get("offerData"), dict) else {}
    currency = str(offer.get("currency") or "")
    source = _common.supplier_amount(offer.get("totalSalePrice"))
    if source is None:
        raise ApiError("quote_unknown", "That fare has no price now. Search flights again.")
    price = fx.display(source, currency)
    verified = fx.to_usd(source, currency)
    changed = verified is None or verified.amount != fare.total.amount
    # A fare that now costs more than its search's limit is outside the travel standard, so its quote goes.
    over_limit = verified is not None and fare.fare_limit is not None and verified.amount > fare.fare_limit
    # An order pays the price the user was last shown; a price with no rate cannot be ordered at all.
    orderable = verified is not None and not over_limit
    if changed:
        quotes.reprice(quote_id, replace(fare, total=verified) if orderable else None)
    result = _common.compact(
        {
            "quote_id": quote_id if orderable else None,
            "price": price,
            "searched_price": fare.total.as_money() if changed else None,
            "fare_limit": {"amount": f"{fare.fare_limit:.2f}", "currency": fx.BASE} if over_limit else None,
            "outside_travel_standard": over_limit or None,
            "journeys": _verify_journeys(data.get("journeys")),
            "baggage": _baggage(offer.get("baggageRules")),
            "refund_rules": _rule_lines(offer.get("refundRules"), currency),
            "change_rules": _rule_lines(offer.get("changeRules"), currency),
            "booking_notice": clean_text(data.get("bookingNotice"), 600),
        }
    )
    if over_limit:
        hint = (
            "This fare now costs more than the travel standard allows for its search (fare_limit), so it can no "
            "longer be ordered. Tell the user, and search flights again."
        )
    elif verified is None:
        hint = (
            "This fare is now priced in a currency with no US dollar rate here, so it can no longer be ordered. "
            "Tell the user, and search flights again."
        )
    elif changed:
        hint = "The price changed since search: quote the current price, which is what an order now costs."
    else:
        hint = "Prices and rules are live from the supplier. Converted prices are approximate."
    return _common.ok(note=_NOTE, **result, hint=hint)


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
