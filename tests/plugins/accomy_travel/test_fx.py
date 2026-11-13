"""US dollars for every price: conversion rounds up to the cent, and the shown shape says when a price is converted."""

from decimal import Decimal

import pytest

from plugins.accomy_travel import fx


def test_usd_is_shown_as_is_without_an_approximate_marker():
    assert fx.display(Decimal(254), "USD") == {"amount": "254.00", "currency": "USD"}


def test_a_converted_price_rounds_up_to_the_cent_and_keeps_its_source():
    converted = fx.to_usd(Decimal(1), "GBP")

    assert converted.amount == Decimal("1.33")
    assert converted.source_amount == Decimal(1)
    assert converted.source_currency == "GBP"
    assert converted.rate == Decimal("1.3242419708")  # 1 / 0.755149, stored to 10 places


def test_a_converted_price_is_marked_approximate_with_its_supplier_amount():
    shown = fx.display(Decimal("238.80"), "GBP")

    assert shown["currency"] == "USD"
    assert shown["approximate"] is True
    assert shown["supplier_price"] == {"amount": "238.80", "currency": "GBP"}


def test_a_currency_without_a_rate_stays_its_own_and_is_unconverted():
    assert fx.to_usd(Decimal(10), "XYZ") is None
    assert fx.display(Decimal(10), "XYZ") == {"amount": "10.00", "currency": "XYZ", "unconverted": True}


def test_an_unconverted_price_sorts_after_every_converted_one():
    converted = fx.display(Decimal(999999), "GBP")
    unconverted = fx.display(Decimal(1), "XYZ")

    assert fx.usd_value(converted) < fx.usd_value(unconverted)


def test_an_unconverted_price_keeps_the_suppliers_own_decimals():
    assert fx.display(Decimal("1.234"), "KWD") == {"amount": "1.234", "currency": "KWD", "unconverted": True}
    assert fx.display(10, "xyz")["currency"] == "XYZ"


@pytest.mark.parametrize("code", ["gbp", " GBP ", "Gbp"])
def test_a_currency_code_is_read_whatever_its_case_or_padding(code):
    assert fx.to_usd(Decimal(10), code) == fx.to_usd(Decimal(10), "GBP")
