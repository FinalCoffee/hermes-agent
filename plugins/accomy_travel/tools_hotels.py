"""Hotel search, details and room rates on the TMC open platform, priced in US dollars.

Nothing here calls an order endpoint. A rate that is shown is remembered as a quote (see quotes.py), which is what
place_order can book.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import _common, fx, geo, quotes, tmc_client
from ._common import ApiError, ToolSpec, clean_text

_TMC_ENV = (tmc_client.APP_KEY_ENV, tmc_client.APP_SECRET_ENV)
# A proximity search reads pages of this size until it has max_results priced hotels, then sorts and caps them.
_SEARCH_PAGE_SIZE = 20
# On some destinations most list rows carry no price (Singapore: 47 of 50), so pages are read until enough are priced.
_MAX_SEARCH_PAGES = 3
_SEARCH_RADIUS_KM = 20
_MAX_NIGHTS = 30
_MAX_PARTY = 9
# TMC's hotel star enum (19 = 5 star, 29 = luxury/quasi-5, 39 = 4 star, ...). Quasi tiers bin to their headline class.
_STAR_CLASS_BY_CODE = {19: 5, 29: 5, 39: 4, 49: 4, 59: 3, 64: 3, 66: 2, 69: 2, 79: 1}
_SORTS = ("distance", "price", "rating")


def _int_arg(args: dict, name: str, default: int, low: int, high: int) -> int:
    raw = args.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw:
        raise ValueError(f"{name} must be a whole number from {low} to {high}.")
    if not low <= int(raw) <= high:
        raise ValueError(f"{name} must be from {low} to {high}.")
    return int(raw)


def _text_arg(args: dict, name: str) -> str:
    value = str(args.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required.")
    return value


def _stay(args: dict) -> tuple[str, str, int]:
    """(check_in, check_out, nights) after the checks that prevent a silent empty from TMC."""
    check_in = tmc_client.parse_date(args.get("check_in"), "check_in")
    check_out = tmc_client.parse_date(args.get("check_out"), "check_out")
    # The server's date can be a day ahead of the user's, so yesterday is allowed and TMC has the final say.
    if check_in < date.today() - timedelta(days=1):
        raise ValueError("check_in is more than a day in the past. Use today or a later date.")
    nights = (check_out - check_in).days
    if nights < 1:
        raise ValueError("check_out must be after check_in.")
    if nights > _MAX_NIGHTS:
        raise ValueError(
            f"The stay can be at most {_MAX_NIGHTS} nights. Split longer trips into shorter searches."
        )
    return check_in.isoformat(), check_out.isoformat(), nights


def _party(args: dict) -> tuple[int, int]:
    """(adults, rooms). Every room must hold the same number of adults: TMC prices one uniform layout."""
    adults = _int_arg(args, "adults", 2, 1, _MAX_PARTY)
    rooms = _int_arg(args, "rooms", 1, 1, _MAX_PARTY)
    if adults % rooms:
        raise ValueError(
            "Adults must divide evenly across rooms, for example 2 adults in 2 rooms or 3 adults in 1 room. "
            "Book uneven layouts as separate searches."
        )
    return adults, rooms


def _map_url(lat: Any, lng: Any) -> str | None:
    if lat is None or lng is None:
        return None
    return f"https://www.google.com/maps/search/?api=1&query={float(lat)},{float(lng)}"


# Hotel names by id. A rate quote carries the name, and get_hotel_rooms reads it from here before it asks TMC.
_NAMES_MAX = 5000
_hotel_names: dict[int, str] = {}


def _note_name(hotel_id: Any, name: str) -> None:
    if isinstance(hotel_id, int) and name:
        if len(_hotel_names) >= _NAMES_MAX:
            _hotel_names.clear()
        _hotel_names[hotel_id] = name


def _hotel_name(hotel_id: int) -> str:
    name = _hotel_names.get(hotel_id)
    if name is None:
        name = clean_text((tmc_client.hotel_detail(hotel_id) or {}).get("hotelName"), 160)
        _note_name(hotel_id, name)
    return name or f"Hotel {hotel_id}"


def _star_codes(min_stars: int | None) -> list[int] | None:
    if min_stars is None:
        return None
    return sorted(
        code for code, stars in _STAR_CLASS_BY_CODE.items() if stars >= min_stars
    )


def _locate(destination: str) -> tuple[float, float, str]:
    """(lat, lng, label) for a destination. Google geocoding when its key is set, otherwise the city row of TMC's
    own location lookup. Google handles any language and gives street-level matches; TMC's lookup is city-level
    but still resolves a city name, so search keeps working without a second key."""
    if _common.has_secrets(geo.GOOGLE_KEY_ENV):
        point = geo.geocode(destination)
        return point.latitude, point.longitude, point.address or destination
    for row in tmc_client.hotel_locations(destination):
        if str(row.get("dataType")) != "2":
            continue
        nested = next(
            (
                item
                for item in row.get("destinationName") or []
                if isinstance(item, dict)
            ),
            {},
        )
        if nested.get("latGoogle") is not None and nested.get("lngGoogle") is not None:
            return (
                float(nested["latGoogle"]),
                float(nested["lngGoogle"]),
                str(nested.get("name") or destination),
            )
    raise ApiError(
        "not_found",
        f"No city named '{destination}' was found. Ask the user for the city name in English "
        "or for a more specific place.",
    )


def _star_class(raw: Any) -> int | None:
    try:
        return _STAR_CLASS_BY_CODE.get(int(raw))
    except (TypeError, ValueError):
        return None


def _hotel_row(raw: dict, nights: int) -> dict | None:
    nightly_source = _common.supplier_amount(raw.get("lowestPrice"))
    if nightly_source is None:
        return None  # no price for these dates, not a free stay
    currency = str(raw.get("saleCurrencyCode") or "")
    row = {
        "hotel_id": raw.get("hotelId"),
        "name": clean_text(raw.get("hotelName"), 160),
        "stars": _star_class(raw.get("hotelStar")),
        "score": raw.get("hotelScore"),
        "brand": clean_text(raw.get("hotelBrandName"), 80),
        "address": clean_text(raw.get("hotelAddress"), 200),
        "distance_km": raw.get("distance"),
        "from_nightly_price": fx.display(nightly_source, currency),
        "image_url": raw.get("hotelImg") or None,
        "map_url": _map_url(raw.get("latGoogle"), raw.get("lngGoogle")),
    }
    row["stay_total_price"] = fx.display(nightly_source * nights, currency)
    return _common.compact(row)


_SORT_KEYS = {
    "distance": lambda row: (
        row.get("distance_km") if row.get("distance_km") is not None else float("inf")
    ),
    "price": lambda row: fx.usd_value(row["from_nightly_price"]),
    "rating": lambda row: -(row.get("score") or 0),
}


def _search_schema() -> dict:
    return {
        "name": "search_hotels",
        "description": (
            "Search hotels with prices in a city or area for given dates, by destination text (any language). "
            "Returns up to max_results hotels with from_nightly_price (the lowest list rate per night) and "
            "stay_total_price, in US dollars. Converted prices are approximate and are a starting point: call "
            "get_hotel_rooms for exact room and rate prices and quote ids."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "destination": {
                    "type": "string",
                    "description": "City, area or landmark, e.g. 'Tokyo' or '東京鐵塔'.",
                },
                "check_in": {
                    "type": "string",
                    "description": "Check-in date, yyyy-mm-dd, today or later.",
                },
                "check_out": {
                    "type": "string",
                    "description": "Check-out date, yyyy-mm-dd, after check_in, max 30 nights.",
                },
                "min_stars": {
                    "type": "integer",
                    "description": "Minimum star class 1-5. Omit unless the user asked.",
                },
                "sort": {
                    "type": "string",
                    "enum": list(_SORTS),
                    "description": "distance (default), price or rating.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Hotels to return, 1-10 (default 5).",
                },
            },
            "required": ["destination", "check_in", "check_out"],
        },
    }


@_common.tool_handler
def search_hotels(args: dict) -> str:
    destination = _text_arg(args, "destination")
    check_in, check_out, nights = _stay(args)
    min_stars = (
        _int_arg(args, "min_stars", 1, 1, 5)
        if args.get("min_stars") is not None
        else None
    )
    sort = str(args.get("sort") or "distance")
    if sort not in _SORTS:
        raise ValueError(f"sort must be one of: {', '.join(_SORTS)}.")
    max_results = _int_arg(args, "max_results", 5, 1, 10)

    lat, lng, label = _locate(destination)
    rows: list[dict] = []
    seen: set = set()
    listed = 0
    total_count = total_pages = None
    page_no = 1
    while True:
        page = tmc_client.hotel_search(
            check_in=check_in,
            check_out=check_out,
            lat=lat,
            lng=lng,
            radius_km=_SEARCH_RADIUS_KM,
            page_size=_SEARCH_PAGE_SIZE,
            page=page_no,
            star_codes=_star_codes(min_stars),
        )
        total_count, total_pages = page["totalCount"], page["totalPage"]
        listed += len(page["hotels"])
        for raw in page["hotels"]:
            _note_name(raw.get("hotelId"), clean_text(raw.get("hotelName"), 160))
            row = _hotel_row(raw, nights)
            # The catalog can shift between page reads, repeating a hotel on the next page.
            if row is not None and row["hotel_id"] not in seen:
                seen.add(row["hotel_id"])
                rows.append(row)
        if len(rows) >= max_results or page_no >= _MAX_SEARCH_PAGES or not _has_next_page(page_no, total_pages):
            break
        page_no += 1
    rows.sort(key=_SORT_KEYS[sort])
    if rows:
        hint = "Pass hotel_id to get_hotel_rooms for rates, or to get_hotel_details for facilities and policies."
    elif listed:
        hint = (
            "Hotels exist near that place, but none had a list price for these dates. Try other dates, "
            "call get_hotel_rooms on a hotel named by the user, or try a nearby city."
        )
    else:
        hint = "No hotels found near that place for these dates. Try other dates or a nearby city."
    return _common.ok(
        note=_common.UNTRUSTED_NOTE,
        search_area=label,
        check_in=check_in,
        check_out=check_out,
        nights=nights,
        total_matches=total_count,
        hotels=rows[:max_results],
        hint=hint,
    )


def _has_next_page(page_no: int, total_pages: Any) -> bool:
    try:
        return page_no < int(total_pages)
    except (TypeError, ValueError):
        return False


def _facilities(raw: list) -> list[str]:
    names = [
        clean_text(item.get("name"), 80)
        for item in raw
        if isinstance(item, dict) and item.get("status") == 1
    ]
    return [name for name in names if name][:20]


def _policies(raw: list) -> list[dict]:
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text") or item.get("textCn"), 300)
        if text:
            out.append(
                {
                    "topic": clean_text(item.get("codeName") or item.get("code"), 60),
                    "text": text,
                }
            )
    return out[:8]


def _images(raw: list) -> list[str]:
    urls = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        for image in entry.get("images") or []:
            if (
                isinstance(image, dict)
                and isinstance(image.get("url"), str)
                and image["url"] not in urls
            ):
                urls.append(image["url"])
    return urls[:5]


def _detail_schema() -> dict:
    return {
        "name": "get_hotel_details",
        "description": (
            "Static details of one hotel from search_hotels: address, check-in and check-out times, phone, "
            "introduction, facilities, policies, photos and room types. Use it to answer 'what is this hotel like'. "
            "Prices come from get_hotel_rooms, not from here. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "hotel_id": {
                    "type": "integer",
                    "description": "hotel_id from search_hotels.",
                }
            },
            "required": ["hotel_id"],
        },
    }


@_common.tool_handler
def get_hotel_details(args: dict) -> str:
    hotel_id = _int_arg(args, "hotel_id", 0, 1, 2**53)
    raw = tmc_client.hotel_detail(hotel_id)
    if not raw:
        raise ApiError(
            "not_found",
            f"Hotel {hotel_id} was not found. Use a hotel_id from search_hotels.",
        )
    _note_name(raw.get("hotelId"), clean_text(raw.get("hotelName"), 160))
    detail = _common.compact(
        {
            "hotel_id": raw.get("hotelId"),
            "name": clean_text(raw.get("hotelName"), 160),
            "stars": _star_class(raw.get("hotelStar")),
            "score": raw.get("hotelScore"),
            "address": clean_text(raw.get("address"), 200),
            "city": clean_text(raw.get("cityName"), 60),
            "country": clean_text(raw.get("countryName"), 60),
            "check_in_time": raw.get("checkInTime"),
            "check_out_time": raw.get("checkOutTime"),
            "phone": raw.get("telephone"),
            "introduction": clean_text(raw.get("hotelIntroduce"), 800),
            "facilities": _facilities(raw.get("hotelFacilityNew") or []),
            "policies": _policies(raw.get("hotelTextPolicies") or []),
            "rooms": [
                _common.compact(
                    {
                        "room_name": clean_text(room.get("roomName"), 120),
                        "max_guests": room.get("maxPerson"),
                        "room_size_sqm": room.get("roomAcreage"),
                    }
                )
                for room in raw.get("roomInfos") or []
                if isinstance(room, dict)
            ][:10],
            "photos": _images(raw.get("hotelImages") or []),
            "map_url": _map_url(raw.get("latGoogle"), raw.get("lngGoogle")),
        }
    )
    return _common.ok(
        note=_common.UNTRUSTED_NOTE,
        hotel=detail,
        hint="Room and rate prices come from get_hotel_rooms with the same hotel_id.",
    )


@dataclass(frozen=True)
class _Stay:
    """The hotel and the stay a rate is quoted for: what an order needs beyond the supplier's rate."""

    hotel_id: int
    hotel_name: str
    check_in: date
    check_out: date
    adults: int
    rooms: int


def _cancellation_note(first: dict) -> str | None:
    """The supplier's cancellation codes as one line, kept with the order. The codes are not for the model."""
    kind = first.get("cancelRestrictionType")
    if kind is None and not first.get("cancelPenalties"):
        return None
    parts = [f"restriction type {kind}"] if kind is not None else []
    if first.get("cancelRestrictionDay") is not None:
        parts.append(f"{first['cancelRestrictionDay']} day(s) before check-in")
    if first.get("cancelRestrictionTime"):
        parts.append(f"until {first['cancelRestrictionTime']}")
    if first.get("cancelPenalties"):
        parts.append("penalties apply")
    return ("Supplier cancellation terms: " + ", ".join(parts))[:500]


def _rate_plan(product: dict, group: dict, stay: _Stay) -> tuple[dict, quotes.HotelRate | None] | None:
    """A rate for the model, and the facts an order needs for it. None when the rate has no price for the party."""
    source = _common.supplier_amount(product.get("totalSalePrice"))
    if source is None:
        return None  # no price for the party, not a free stay
    items = [item for item in product.get("priceItems") or [] if isinstance(item, dict)]
    currency = str(product.get("currency") or next(
        (item.get("currency") for item in items if item.get("currency")), "") or "")
    breakfasts = [
        int(item["breakfastNum"])
        for item in items
        if item.get("breakfastNum") is not None
    ]
    first = items[0] if items else {}
    average = _common.supplier_amount(product.get("avgPrice"))
    view = _common.compact(
        {
            "rate_plan": clean_text(product.get("ratePlanName"), 120),
            "total_price": fx.display(source, currency),
            "avg_nightly_price": fx.display(average, currency) if average is not None else None,
            "breakfast_per_day": min(breakfasts) if breakfasts else None,
            "bed": clean_text(product.get("bedInfoDesc"), 120),
            "max_guests": product.get("maxPerson"),
            "cancellation": _common.compact(
                {
                    "restriction_type": first.get("cancelRestrictionType"),
                    "restriction_days": first.get("cancelRestrictionDay"),
                    "restriction_time": first.get("cancelRestrictionTime"),
                    "penalties_listed": bool(first.get("cancelPenalties")),
                }
            ),
        }
    )
    total = fx.to_usd(source, currency)
    rate_plan_id = str(product.get("ratePlanId") or "")
    if total is None or not rate_plan_id:
        return view, None
    return view, quotes.HotelRate(
        hotel_id=stay.hotel_id,
        hotel_name=stay.hotel_name,
        room_id=group.get("roomId"),
        room_name=clean_text(group.get("roomName"), 160),
        rate_plan_id=rate_plan_id,
        rate_plan_name=clean_text(product.get("ratePlanName"), 120),
        supply_code=str(product.get("supplyCode") or "") or None,
        check_in=stay.check_in,
        check_out=stay.check_out,
        adults=stay.adults,
        rooms=stay.rooms,
        cancellation=_cancellation_note(first),
        total=total,
    )


def _room_row(group: dict, stay: _Stay) -> dict | None:
    priced = [
        pair
        for pair in (
            _rate_plan(p, group, stay) for p in group.get("products") or [] if isinstance(p, dict)
        )
        if pair is not None
    ][:4]
    if not priced:
        return None
    rates = []
    for view, facts in priced:
        if facts is not None:
            view["quote_id"] = quotes.remember(quotes.HOTEL, facts)
        rates.append(view)
    return _common.compact(
        {
            "room_id": group.get("roomId"),
            "room_name": clean_text(group.get("roomName"), 160),
            "photo_url": group.get("roomImage") or None,
            "rates": rates,
        }
    )


def _rooms_schema() -> dict:
    return {
        "name": "get_hotel_rooms",
        "description": (
            "Room types and rate plans with prices in US dollars for one hotel, dates and party. total_price is for the "
            "whole stay for the whole party; avg_nightly_price is per night. Converted prices are approximate. "
            "breakfast_per_day counts breakfasts; the cancellation fields are supplier codes, so confirm the terms with "
            "the hotel. Each rate with a quote_id can be booked with place_order."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "hotel_id": {
                    "type": "integer",
                    "description": "hotel_id from search_hotels.",
                },
                "check_in": {
                    "type": "string",
                    "description": "Check-in date, yyyy-mm-dd, today or later.",
                },
                "check_out": {
                    "type": "string",
                    "description": "Check-out date, yyyy-mm-dd, after check_in, max 30 nights.",
                },
                "adults": {
                    "type": "integer",
                    "description": "Adults in the whole party, 1-9 (default 2).",
                },
                "rooms": {
                    "type": "integer",
                    "description": "Rooms, 1-9 (default 1). Adults must split evenly.",
                },
            },
            "required": ["hotel_id", "check_in", "check_out"],
        },
    }


@_common.tool_handler
def get_hotel_rooms(args: dict) -> str:
    hotel_id = _int_arg(args, "hotel_id", 0, 1, 2**53)
    check_in, check_out, nights = _stay(args)
    adults, rooms = _party(args)
    groups = tmc_client.hotel_products(
        hotel_id=hotel_id,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        rooms=rooms,
    )
    stay = _Stay(
        hotel_id=hotel_id,
        hotel_name=_hotel_name(hotel_id),
        check_in=date.fromisoformat(check_in),
        check_out=date.fromisoformat(check_out),
        adults=adults,
        rooms=rooms,
    )
    room_rows = [
        row for row in (_room_row(group, stay) for group in groups) if row is not None
    ]
    hint = (
        "Totals are for the whole stay and party in US dollars; converted prices are approximate. Only rates "
        "with a quote_id can be booked, and quote ids expire after 30 minutes."
        if room_rows
        else "No rooms with a price for this party and these dates. Try other dates or fewer guests."
    )
    return _common.ok(
        note=_common.UNTRUSTED_NOTE,
        hotel_id=hotel_id,
        check_in=check_in,
        check_out=check_out,
        nights=nights,
        adults=adults,
        rooms=rooms,
        room_types=room_rows[:8],
        hint=hint,
    )


TOOLS = (
    ToolSpec(
        name="search_hotels",
        schema=_search_schema(),
        handler=search_hotels,
        requires_env=_TMC_ENV,
        emoji="🏨",
    ),
    ToolSpec(
        name="get_hotel_details",
        schema=_detail_schema(),
        handler=get_hotel_details,
        requires_env=_TMC_ENV,
        emoji="🏨",
    ),
    ToolSpec(
        name="get_hotel_rooms",
        schema=_rooms_schema(),
        handler=get_hotel_rooms,
        requires_env=_TMC_ENV,
        emoji="🛏️",
    ),
)
