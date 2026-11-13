"""Every price the bot shows and every order it takes is in US dollars.

Suppliers quote in their own currencies (Seat Unique mostly in GBP and EUR). Amounts are converted with
one fixed table of reference rates, which is demo quality: close, never exact, and never refreshed. The
shape follows hashtrip's ``c_fx_rate``: a rate is "1 unit of the base currency buys ``rate`` units of
the quote currency", a pair is derived from the two USD quotes, and an amount someone is charged is
rounded UP to the cent. An order keeps the supplier's amount and the rate it was converted at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from decimal import ROUND_CEILING, Decimal

BASE = "USD"
PROVIDER = "demo-static"
# Units of each currency that one US dollar bought on the reference date (exchangerate-api.com).
# AS_OF names this exact table: change it whenever a rate below changes. An order records
# (pair, PROVIDER, AS_OF) once, and the store refuses the same instant with another rate.
AS_OF = datetime(2026, 11, 13, tzinfo=UTC)
_PER_USD = {
    "AED": "3.6725", "AUD": "1.468309", "BRL": "5.610238", "CAD": "1.42563", "CHF": "0.83437",
    "CNY": "6.685071", "CZK": "22.038149", "DKK": "6.747617", "EUR": "0.903491", "GBP": "0.755149",
    "HKD": "7.844955", "HUF": "329.951279", "IDR": "18250.530965", "ILS": "3.195539", "INR": "94.086126",
    "JPY": "159.795417", "KRW": "1334.182801", "MXN": "18.620415", "MYR": "4.078759", "NOK": "9.56065",
    "NZD": "1.802654", "PHP": "63.786172", "PLN": "3.981443", "QAR": "3.64", "SAR": "3.75",
    "SEK": "10.018002", "SGD": "1.286283", "THB": "33.855133", "TRY": "50.311711", "TWD": "32.976133",
    "VND": "25690.620978", "ZAR": "16.735572",
}
_CENT = Decimal("0.01")
_RATE_SCALE = Decimal("0.0000000001")  # numeric(20,10), as c_fx_rate.rate


@dataclass(frozen=True)
class Converted:
    """A supplier amount in USD, with what it was converted from."""

    amount: Decimal
    source_amount: Decimal
    source_currency: str
    rate: Decimal

    def as_money(self) -> dict:
        """The tool-result shape of a price: USD, marked approximate when it was converted."""
        money: dict = {"amount": f"{self.amount:.2f}", "currency": BASE}
        if self.source_currency != BASE:
            money["approximate"] = True
            money["supplier_price"] = {"amount": str(self.source_amount), "currency": self.source_currency}
        return money


def usd_rate(currency: str) -> Decimal | None:
    """How many US dollars one unit of *currency* buys; None for a currency the table does not list."""
    if currency == BASE:
        return Decimal(1)
    per_usd = _PER_USD.get(currency)
    return (Decimal(1) / Decimal(per_usd)).quantize(_RATE_SCALE) if per_usd else None


def to_usd(amount: Decimal | str | float, currency: str | None) -> Converted | None:
    """*amount* of *currency* in US dollars, rounded up to the cent; None when there is no rate."""
    currency = (currency or "").strip().upper()
    rate = usd_rate(currency)
    if rate is None:
        return None
    source = Decimal(str(amount))
    return Converted((source * rate).quantize(_CENT, rounding=ROUND_CEILING), source, currency, rate)


def usd_value(money: dict) -> Decimal:
    """The USD amount of a displayed price, for sorting. An unconverted price sorts after every converted one."""
    return Decimal("Infinity") if money.get("unconverted") else Decimal(money["amount"])


def display(amount: Decimal | str | float, currency: str | None) -> dict:
    """The price a tool shows. A currency the table lacks keeps its own amount, marked unconverted, and such a
    price cannot be ordered."""
    converted = to_usd(amount, currency)
    if converted is not None:
        return converted.as_money()
    own = Decimal(str(amount))
    decimals = max(2, -own.as_tuple().exponent)  # the supplier's own precision: three for KWD, never fewer than two
    return {"amount": f"{own:.{decimals}f}", "currency": (currency or "").strip().upper(), "unconverted": True}
