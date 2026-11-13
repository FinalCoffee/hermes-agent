"""The demonstration travel standard: what a hotel night may cost and which flights may be booked.

The rules have the shape of a travel policy on the TMC platform the searches come from: a nightly hotel budget
with a budget of its own for named cities (maxAmount, policyDestList), a lowest-price rule for air fares
(budgetPercentage), and cabin classes allowed from a minimum flight duration that leaves out the time on the
ground at a stop (minFlightDurationHours, excludeStopoverTime). The bot applies them itself. Nothing is read
from or written to the tenant's own policies in TMC, so the standard is the same under any TMC key.

The hotel and flight tools leave out what is outside the standard, so it gets no quote and cannot be ordered.
policy.py renders these numbers into the system prompt, which is how the assistant explains the standard.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

# US dollars for one room for one night. A rate is within it when no night costs more and the stay's total,
# which carries any taxes, does not average more.
HOTEL_NIGHTLY_BUDGET = Decimal(200)
HOTEL_CITY_NIGHTLY_BUDGET = Decimal(300)
# TMC city codes, as hotel rows and hotel details carry them (cityCode), pinned by a live lookup. A hotel in
# another city of the same name, or with no city code, gets the general budget.
HOTEL_BUDGET_CITIES = {"2114": "London", "2734": "Paris", "2621": "New York"}

# A fare may cost at most this much more than the cheapest fare of the same search.
FARE_BUDGET_PERCENTAGE = 70

ECONOMY = "economy"
PREMIUM_CABINS = ("premium_economy", "business")
CABINS = (ECONOMY, *PREMIUM_CABINS)  # first class is never within the standard
PREMIUM_CABIN_MIN_HOURS = 8

_CENT = Decimal("0.01")


def nightly_budget(city_code: object) -> Decimal:
    """The nightly budget for a hotel in the city with this TMC city code."""
    return HOTEL_CITY_NIGHTLY_BUDGET if str(city_code or "") in HOTEL_BUDGET_CITIES else HOTEL_NIGHTLY_BUDGET


def cabin_allowed(cabin: str, flying_minutes: int | None) -> bool:
    """Whether a flight of this flying time may be booked in *cabin*. A flight whose flying time is unknown
    counts as a short one."""
    if cabin == ECONOMY:
        return True
    return (cabin in PREMIUM_CABINS and flying_minutes is not None
            and flying_minutes >= PREMIUM_CABIN_MIN_HOURS * 60)


def fare_limit(lowest: Decimal) -> Decimal:
    """The dearest fare the standard allows in a search whose cheapest fare is *lowest*, to the cent."""
    return (lowest * (100 + FARE_BUDGET_PERCENTAGE) / 100).quantize(_CENT, rounding=ROUND_DOWN)


def describe() -> str:
    """The standard in words, for the system prompt."""
    *first, last = HOTEL_BUDGET_CITIES.values()
    premium = " or ".join(cabin.replace("_", " ") for cabin in PREMIUM_CABINS)
    return (
        f"- Hotels: at most {HOTEL_NIGHTLY_BUDGET} US dollars per room for any night; "
        f"{HOTEL_CITY_NIGHTLY_BUDGET} in {', '.join(first)} and {last}.\n"
        f"- Flights under {PREMIUM_CABIN_MIN_HOURS} hours of flying time (time on the ground at a stop is not "
        "counted): economy only.\n"
        f"- Flights of {PREMIUM_CABIN_MIN_HOURS} hours or more: economy, {premium}. Never first class.\n"
        f"- Fares: at most {FARE_BUDGET_PERCENTAGE}% above the cheapest fare of the same search (same route, "
        "date and cabin).\n"
    )
