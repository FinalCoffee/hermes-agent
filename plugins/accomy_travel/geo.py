"""Place name to coordinates (Google Geocoding API), shared by the place, weather and hotel tools.

Hotel search resolves through coordinates because the supplier's own location lookup returns
hotels and points of interest ahead of the city for non-English names.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import _common

GOOGLE_KEY_ENV = "GOOGLE_MAPS_SERVER_API_KEY"
_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float
    address: str
    place_id: str


def geocode(query: str, language: str = "en") -> GeoPoint:
    """Best match for *query*; raises ``ApiError('not_found')`` when Google has none."""
    query = (query or "").strip()
    if not query:
        raise ValueError("A place name or address is required.")
    status, body = _common.request_json(
        "GET", _GEOCODE_URL,
        params={"address": query, "language": language, "key": _common.secret(GOOGLE_KEY_ENV)})
    result_status = body.get("status") if isinstance(body, dict) else None
    if result_status == "ZERO_RESULTS":
        raise _common.ApiError("not_found", f"No location found for '{query}'. Ask the user for a more specific place.")
    if status != 200 or result_status != "OK":
        raise _common.ApiError("geocode_failed", "The location lookup failed. Try again shortly.", status=status)
    top = body["results"][0]
    location = top["geometry"]["location"]
    return GeoPoint(
        latitude=float(location["lat"]), longitude=float(location["lng"]),
        address=top.get("formatted_address", ""), place_id=top.get("place_id", ""))
