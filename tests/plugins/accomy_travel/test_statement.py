"""What an order's statement says, from orders shaped as the store returns them. No database and no PDF here."""

import datetime as dt
from decimal import Decimal

import pytest

from plugins.accomy_travel import statement
from plugins.accomy_travel.orderstore import store

ISSUED = dt.date(2026, 11, 14)
MEI = {"given_name": "Mei", "family_name": "Lin", "traveller_kind": "ADULT"}


def _item(kind: str, amount: str, detail: dict, *, travellers=(), quantity: int = 1, conversion=None) -> store.Item:
    return store.Item(
        id=1, seq=1, kind=kind, source="TMC", title="as placed", quantity=quantity, sell_amount=Decimal(amount),
        booking_status="CONFIRMED", fulfilment_status="NOT_APPLICABLE", supplier_order_no=None, supplier_ref=None,
        supplier_confirm_no=None, pay_by_at=None, version=1, detail=detail, travellers=tuple(travellers),
        conversion=conversion)


def _flight(number: str, journey: int, origin: str, destination: str, depart: str, arrive: str,
            cabin: str | None = "Economy") -> dict:
    return {"segment_seq": 1, "journey_seq": journey, "carrier_code": number[:2], "flight_no": number,
            "origin_code": origin, "destination_code": destination, "cabin_class": cabin,
            "depart_local": dt.datetime.fromisoformat(depart), "arrive_local": dt.datetime.fromisoformat(arrive)}


def _flights(kind: str, *segments: dict, adults: int = 1, amount: str = "326.00", travellers=(MEI,)) -> store.Item:
    detail = {"trip_kind": kind, "adult_count": adults, "child_count": 0, "infant_count": 0, "segments": list(segments)}
    return _item("FLIGHT", amount, detail, travellers=travellers, quantity=adults)


def _hotel(check_in: str = "2026-11-18", check_out: str = "2026-11-20", *, rooms: int = 1, adults: int = 2,
           room: str | None = "Deluxe King Room", amount: str = "360.00", **more) -> store.Item:
    detail = {"hotel_name": "The Standard, Bangkok Mahanakhon", "room_name": room, "room_count": rooms,
              "adult_count": adults, "child_count": 0, "check_in_date": dt.date.fromisoformat(check_in),
              "check_out_date": dt.date.fromisoformat(check_out)}
    return _item("HOTEL", amount, detail, travellers=(MEI,), **more)


def _tickets(starts: str | None = "2026-12-22T19:30", quantity: int = 2) -> store.Item:
    detail = {"event_name": "Test Final", "category_name": "Hospitality", "venue_name": "Wembley Stadium",
              "starts_local": dt.datetime.fromisoformat(starts) if starts else None, "tz_name": "Europe/London"}
    return _item("EVENT_TICKET", "477.60", detail, quantity=quantity)


def _order(*items: store.Item, total: str = "326.00", contact=store.Contact("Mei Lin", "mei@example.com", None),
           placed: str = "2026-11-14T03:20:00+00:00", trip=("Singapore in November", "2026-11-16", "2026-11-22"),
           ) -> store.Booking:
    title, starts, ends = trip
    return store.Booking(
        id=1, code="ACF274KNM28JHV", buyer=store.Buyer("telegram", "1001", "1001"), contact=contact, locale="zh-TW",
        currency="USD", status="CONFIRMED", total_amount=Decimal(total), version=1,
        created_at=dt.datetime.fromisoformat(placed), order_mode="SIMULATED", items=items,
        trip=store.Trip("TR8K2M4N6P8Q0R", title, starts and dt.date.fromisoformat(starts),
                        ends and dt.date.fromisoformat(ends), "ACTIVE"))


def _statement(*items: store.Item, payment: str | None = "UNPAID", **order) -> statement.Statement:
    return statement.build(_order(*items, **order), payment=payment, issued=ISSUED)


def _column(columns, label: str) -> tuple[str, ...]:
    return next(column.lines for column in columns if column.label == label)


# ── The items ─────────────────────────────────────────────────────────────────


def test_a_round_trip_is_one_line_that_names_each_leg_and_lists_every_flight():
    out = _flight("TR897", 1, "TPE", "SIN", "2026-11-16T08:05", "2026-11-16T12:40")
    back = _flight("TR898", 2, "SIN", "TPE", "2026-11-22T13:55", "2026-11-22T18:35")

    line, = _statement(_flights("ROUND_TRIP", out, back)).lines

    assert line.title == "TPE to SIN, SIN to TPE"
    assert line.notes == (
        "Round trip",
        "TR897 · Mon 16 Nov · 08:05 TPE to 12:40 SIN · Economy",
        "TR898 · Sun 22 Nov · 13:55 SIN to 18:35 TPE · Economy")
    assert (line.dates, line.quantity, line.amount) == ("16–22 Nov 2026", "1 passenger", "326.00")


def test_a_leg_with_a_connection_is_named_by_where_it_starts_and_where_it_ends():
    first = _flight("CX450", 1, "TPE", "HKG", "2026-12-10T07:00", "2026-12-10T08:55")
    second = _flight("CX251", 1, "HKG", "LHR", "2026-12-10T10:30", "2026-12-10T15:40", cabin=None)

    line, = _statement(_flights("ONE_WAY", first, second, adults=3)).lines

    assert line.title == "TPE to LHR"
    assert line.notes[0] == "One way"
    assert line.notes[2] == "CX251 · Thu 10 Dec · 10:30 HKG to 15:40 LHR"  # no cabin known: none is claimed
    assert line.quantity == "3 passengers"


@pytest.mark.parametrize("stored, shown", [("economy", "Economy"), ("premium_economy", "Premium economy"),
                                           ("BUSINESS", "Business")])
def test_a_cabin_reads_as_words_however_the_supplier_wrote_it(stored, shown):
    flight = _flight("TR897", 1, "TPE", "SIN", "2026-11-16T08:05", "2026-11-16T12:40", cabin=stored)

    line, = _statement(_flights("ONE_WAY", flight)).lines

    assert line.notes[1].endswith(f"SIN · {shown}")


def test_a_flight_that_lands_on_another_day_says_which():
    overnight = _flight("MM029", 2, "KIX", "KHH", "2026-12-20T23:15", "2026-12-21T01:40")
    first = _flight("MM620", 1, "TPE", "NRT", "2026-12-10T02:25", "2026-12-10T06:30")

    line, = _statement(_flights("MULTI_CITY", first, overnight)).lines

    assert line.title == "TPE to NRT, KIX to KHH"
    assert line.notes[0] == "Multi-city"
    assert line.notes[2] == "MM029 · Sun 20 Dec · 23:15 KIX to 01:40 KHH on 21 Dec · Economy"
    assert line.dates == "10–21 Dec 2026"


def test_a_hotel_stay_is_counted_in_nights_with_its_room_and_guests():
    line, = _statement(_hotel(rooms=2, adults=3)).lines

    assert line.title == "The Standard, Bangkok Mahanakhon"
    assert line.notes == ("Deluxe King Room · 2 rooms · 3 guests",)
    assert (line.dates, line.quantity, line.amount) == ("18–20 Nov 2026", "2 nights", "360.00")


def test_a_stay_of_one_night_for_one_guest_in_a_room_without_a_name():
    line, = _statement(_hotel("2026-11-18", "2026-11-19", adults=1, room=None)).lines

    assert line.notes == ("1 room · 1 guest",)
    assert line.quantity == "1 night"


def test_event_tickets_show_the_category_the_venue_and_the_local_start():
    line, = _statement(_tickets()).lines

    assert line.title == "Test Final"
    assert line.notes == ("Hospitality · Wembley Stadium", "Starts 19:30, local time")
    assert (line.dates, line.quantity, line.amount) == ("22 Dec 2026", "2 tickets", "477.60")


def test_an_event_without_a_start_time_claims_no_date():
    line, = _statement(_tickets(starts=None, quantity=1)).lines

    assert line.notes == ("Hospitality · Wembley Stadium",)
    assert (line.dates, line.quantity) == ("—", "1 ticket")


@pytest.mark.parametrize("source, shown", [("12960", "THB 12,960"), ("238.80", "THB 238.80")])
def test_a_converted_price_names_the_suppliers_price_and_says_it_is_approximate(source, shown):
    rate = store.FxConversion(Decimal(source), "THB", Decimal("0.0277"), dt.datetime(2026, 11, 1, tzinfo=dt.UTC), "fixed")

    line, = _statement(_hotel(conversion=rate)).lines

    assert line.notes[-1] == f"Converted from {shown}: an approximate price"
    assert line.amount == "360.00"


@pytest.mark.parametrize("check_in, check_out, shown", [
    ("2026-11-18", "2026-11-20", "18–20 Nov 2026"),
    ("2026-11-30", "2026-12-02", "30 Nov–2 Dec 2026"),
    ("2026-12-30", "2027-01-02", "30 Dec 2026–2 Jan 2027"),
])
def test_a_range_of_days_names_a_shared_month_and_year_once(check_in, check_out, shown):
    line, = _statement(_hotel(check_in, check_out)).lines

    assert line.dates == shown


def test_an_order_of_several_items_keeps_them_in_order_under_one_total():
    out = _flight("TR897", 1, "TPE", "SIN", "2026-11-16T08:05", "2026-11-16T12:40")

    page = _statement(_flights("ONE_WAY", out, amount="1163.00"), _hotel(), _tickets(), total="2000.60")

    assert [line.amount for line in page.lines] == ["1,163.00", "360.00", "477.60"]
    assert (page.currency, page.total) == ("USD", "2,000.60")


# ── The people ────────────────────────────────────────────────────────────────


def test_travellers_are_listed_once_each_family_name_first():
    people = (MEI, {"given_name": "小明", "family_name": "王", "traveller_kind": "ADULT"})
    out = _flight("TR897", 1, "TPE", "SIN", "2026-11-16T08:05", "2026-11-16T12:40")

    page = _statement(_flights("ONE_WAY", out, adults=2, travellers=people), _hotel())

    assert _column(page.parties, "Travellers") == ("LIN Mei", "王 小明")


def test_two_travellers_of_one_name_are_two_lines_and_one_traveller_on_two_items_is_one():
    twins = (MEI, MEI, {"given_name": "Wei", "family_name": "Chen", "traveller_kind": "ADULT"})
    out = _flight("TR897", 1, "TPE", "SIN", "2026-11-16T08:05", "2026-11-16T12:40")

    page = _statement(_flights("ONE_WAY", out, adults=3, travellers=twins), _hotel())  # the hotel names Mei once

    assert _column(page.parties, "Travellers") == ("LIN Mei", "LIN Mei", "CHEN Wei")


def test_an_order_of_tickets_alone_names_no_traveller():
    assert _column(_statement(_tickets()).parties, "Travellers") == ("—",)


def test_the_contact_is_who_ordered_and_a_missing_detail_leaves_no_gap():
    page = _statement(_hotel(), contact=store.Contact("Mei Lin", None, "+886 912 345 678"))

    assert _column(page.parties, "Ordered by") == ("Mei Lin", "+886 912 345 678")


def test_an_order_whose_contact_was_erased_still_has_a_statement():
    page = _statement(_hotel(), contact=store.Contact(None, None, None))

    assert _column(page.parties, "Ordered by") == ("—",)


# ── What it says of the order itself ──────────────────────────────────────────


@pytest.mark.parametrize("payment, status, tone, first_line", [
    ("PAID", "Demonstration · paid", "good", "Demonstration payment made"),
    ("UNPAID", "Demonstration · not paid", "open", "Not paid"),
    (None, "Demonstration order", "open", "Payment not known"),  # the payment page could not be asked
    ("VOID", "Demonstration order", "open", "Payment not known"),
])
def test_the_status_says_demonstration_whatever_is_known_of_the_payment(payment, status, tone, first_line):
    page = _statement(_hotel(), payment=payment)

    assert (page.status, page.status_tone) == (status, tone)
    assert _column(page.summary, "Payment")[0] == first_line


def test_a_paid_demonstration_says_that_nothing_was_charged():
    assert _column(_statement(_hotel(), payment="PAID").summary, "Payment")[1] == "Nothing was charged"


def test_an_order_whose_payment_cannot_be_asked_about_is_called_neither_paid_nor_unpaid():
    # It may have been paid a minute ago: the page says only what holds either way.
    lines = _column(_statement(_hotel(), payment=None).summary, "Payment")

    assert lines == ("Payment not known", "A demonstration: nothing is charged")


def test_the_reference_is_the_booking_code_and_the_days_are_utc_days():
    # 07:30 on the 15th in Taipei is still the 14th in UTC.
    page = _statement(_hotel(), placed="2026-11-15T07:30:00+08:00")

    assert page.reference == "ACF274KNM28JHV"
    assert page.issued == "14 Nov 2026 UTC"
    assert _column(page.summary, "Order") == (
        "Demonstration order", "Not placed with a supplier", "Placed 14 Nov 2026 UTC")


def test_the_trip_is_shown_with_its_title_its_days_and_its_code():
    page = _statement(_hotel(), trip=("2026年11月16日至22日新加坡來回之旅", "2026-11-16", "2026-11-22"))

    assert _column(page.summary, "Trip") == ("2026年11月16日至22日新加坡來回之旅", "16–22 Nov 2026", "Trip TR8K2M4N6P8Q0R")


def test_a_trip_without_days_shows_none():
    page = _statement(_tickets(starts=None), trip=("Some day", None, None))

    assert _column(page.summary, "Trip") == ("Some day", "Trip TR8K2M4N6P8Q0R")
