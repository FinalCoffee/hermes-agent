"""Simulated orders, exchange rates and cancellation, against a real PostgreSQL 17 in a throwaway schema.

Needs the same local test database as test_orderstore.py; without it every test here is skipped.
"""

import asyncio
import secrets
import socket
from datetime import date, datetime, timezone, UTC
from decimal import Decimal

import pytest

asyncpg = pytest.importorskip("asyncpg")

from plugins.accomy_travel.orderstore import db, migrate, store

HOST, PORT = "127.0.0.1", 55433
DSN = f"postgresql://chatbot:accomy-test-only@{HOST}:{PORT}/chatbot"
ALICE = store.Buyer("telegram", "1001", "1001")
BOB = store.Buyer("telegram", "2002", "2002")
AS_OF = datetime(2026, 11, 13, tzinfo=UTC)


def _database_is_up() -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((HOST, PORT)) == 0


pytestmark = pytest.mark.skipif(not _database_is_up(), reason=f"no test PostgreSQL on {HOST}:{PORT}")


@pytest.fixture(scope="module")
def schema():
    name = f"acc_s_{secrets.token_hex(6)}"

    async def build():
        conn = await db.connect(DSN, schema=name)
        try:
            await migrate.migrate(conn, commit=True, schema=name)
        finally:
            await conn.close()

    async def drop():
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        finally:
            await conn.close()

    asyncio.run(build())
    yield name
    asyncio.run(drop())


def run(schema, work):
    """Run ``work(conn)`` on a fresh connection to the test schema."""

    async def go():
        conn = await db.connect(DSN, schema=schema)
        try:
            return await work(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


def stay(amount="254.00", source=None, rate=None, currency="USD", as_of=AS_OF):
    conversion = None
    if source is not None:
        conversion = store.FxConversion(Decimal(source), currency, Decimal(rate), as_of, "demo-static")
    return store.NewItem(
        source="TMC", title="Park Hotel Tokyo / Deluxe King", sell_amount=Decimal(amount),
        detail=store.HotelStay(
            hotel_id="H123", hotel_name="Park Hotel Tokyo", room_id="R9", room_name="Deluxe King",
            check_in=date(2027, 3, 1), check_out=date(2027, 3, 4), adults=2),
        travellers=(store.Traveller("ADULT", given_name="Mei", family_name="Lin"),),
        conversion=conversion)


def simulated(*items, buyer=ALICE, client_key=None, mode="SIMULATED"):
    return store.NewBooking(
        buyer=buyer, currency="USD", locale="zh-TW", items=items or (stay(),),
        contact=store.Contact("Mei Lin", email="mei@example.com"), client_key=client_key, mode=mode)


def place(schema, new):
    """Create and confirm a simulated order in one transaction, as the order tools do."""
    async def work(conn):
        return await db.with_tx(conn, lambda c: _create_and_confirm(c, new))

    return run(schema, work)


async def _create_and_confirm(conn, new):
    booking = await store.create_booking(conn, new, actor="test")
    if booking.replayed:
        return booking
    return await store.confirm_simulated(conn, booking, actor="test")


def create(schema, new):
    return run(schema, lambda conn: db.with_tx(conn, lambda c: store.create_booking(c, new, actor="test")))


def count(schema, sql, *args):
    return run(schema, lambda conn: conn.fetchval(sql, *args))


# ── Confirming a simulated order ──────────────────────────────────────────────


def test_a_simulated_order_is_confirmed_with_no_supplier_order(schema):
    booking = place(schema, simulated(stay("254.00"), stay("100.00")))

    assert (booking.status, booking.order_mode) == ("CONFIRMED", "SIMULATED")
    assert {(item.booking_status, item.fulfilment_status) for item in booking.items} == {("CONFIRMED", "NOT_APPLICABLE")}
    assert all(item.supplier_order_no is None and item.supplier_ref is None for item in booking.items)
    assert count(schema, "SELECT count(*) FROM o_supplier_request WHERE item_id = ANY($1)",
                 [item.id for item in booking.items]) == 0


def test_a_simulated_order_cannot_be_sent_to_a_supplier_even_by_hand(schema):
    booking = place(schema, simulated())
    item = booking.items[0]

    async def work(conn):
        return await db.with_tx(conn, lambda c: store.open_supplier_request(
            c, booking, item, "PLACE", actor="test"))

    with pytest.raises(asyncpg.PostgresError, match="simulated order and has no supplier"):
        run(schema, work)


def test_only_a_simulated_draft_is_confirmed_by_the_simulation_path(schema):
    booking = create(schema, simulated(mode="SUPPLIER"))

    async def work(conn):
        return await store.confirm_simulated(conn, booking, actor="test")

    with pytest.raises(store.OrderError) as refused:
        run(schema, lambda conn: db.with_tx(conn, lambda c: work(c)))
    assert refused.value.code == "bad_mode"


def test_an_unknown_mode_is_refused(schema):
    with pytest.raises(store.OrderError) as refused:
        create(schema, simulated(mode="PRETEND"))
    assert refused.value.code == "bad_mode"


def test_a_simulated_order_is_marked_in_the_database_and_read_back(schema):
    booking = place(schema, simulated())

    read = run(schema, lambda conn: store.get_booking(conn, booking.code, buyer=ALICE))
    assert read.order_mode == "SIMULATED"
    assert count(schema, "SELECT count(*) FROM o_booking WHERE order_mode = 'SIMULATED' AND id = $1", booking.id) == 1


# ── Exchange rates ────────────────────────────────────────────────────────────


def test_a_rate_is_written_once_and_each_item_keeps_it(schema):
    booking = place(schema, store.NewBooking(
        buyer=BOB, currency="USD", locale="zh-TW", mode="SIMULATED",
        items=(stay("100.00", source="75.50", rate="1.3243000000", currency="GBP"),
               stay("200.00", source="151.00", rate="1.3243000000", currency="GBP")),
        contact=store.Contact("Mei Lin", email="mei@example.com")))

    rows = run(schema, lambda conn: conn.fetch(
        "SELECT id, rate, base_currency, quote_currency FROM c_fx_rate WHERE base_currency = 'GBP'"))
    assert len(rows) == 1
    assert (rows[0]["base_currency"], rows[0]["quote_currency"]) == ("GBP", "USD")
    assert [item.conversion.rate for item in booking.items] == [Decimal("1.3243000000")] * 2
    assert booking.items[0].conversion.source_amount == Decimal("75.50")
    assert booking.items[0].conversion.source_currency == "GBP"


def test_the_same_instant_with_another_rate_is_refused(schema):
    create(schema, store.NewBooking(buyer=ALICE, currency="USD", locale="zh-TW", items=(
        stay("100.00", source="75.50", rate="1.3243000000", currency="GBP"),), mode="SIMULATED"))

    with pytest.raises(store.OrderError) as refused:
        create(schema, store.NewBooking(buyer=ALICE, currency="USD", locale="zh-TW", items=(
            stay("100.00", source="75.50", rate="1.3300000000", currency="GBP"),), mode="SIMULATED"))
    assert refused.value.code == "bad_rate"


def test_a_conversion_into_the_same_currency_is_refused(schema):
    with pytest.raises(store.OrderError) as refused:
        create(schema, simulated(stay("100.00", source="100.00", rate="1.0", currency="USD")))
    assert refused.value.code == "bad_rate"


def test_a_supplier_amount_with_more_decimals_than_the_column_keeps_is_refused(schema):
    with pytest.raises(store.OrderError) as refused:
        create(schema, simulated(stay("100.00", source="75.123456", rate="1.3243000000", currency="GBP")))
    assert refused.value.code == "bad_rate"


def test_a_conversion_cannot_be_changed_after_it_is_written(schema):
    booking = place(schema, simulated(stay("100.00", source="75.50", rate="1.3243000000", currency="GBP")))

    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        run(schema, lambda conn: conn.execute(
            "UPDATE o_booking_item SET source_amount = 1 WHERE id = $1", booking.items[0].id))


def test_the_order_mode_cannot_be_changed(schema):
    booking = place(schema, simulated())

    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        run(schema, lambda conn: conn.execute(
            "UPDATE o_booking SET order_mode = 'SUPPLIER' WHERE id = $1", booking.id))


def test_rates_are_append_only_and_never_truncated(schema):
    place(schema, simulated(stay("100.00", source="75.50", rate="1.3243000000", currency="GBP")))

    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        run(schema, lambda conn: conn.execute("UPDATE c_fx_rate SET rate = 2"))
    # The items refer to the rates, so PostgreSQL refuses the truncate before the guard trigger even runs.
    with pytest.raises(asyncpg.PostgresError):
        run(schema, lambda conn: conn.execute("TRUNCATE c_fx_rate"))


# ── Repeats and request keys ──────────────────────────────────────────────────


def test_a_repeat_of_a_simulated_request_returns_the_first_order(schema):
    first = place(schema, simulated(client_key="order-1"))
    again = place(schema, simulated(client_key="order-1"))

    assert again.replayed is True
    assert again.code == first.code
    assert count(schema, "SELECT count(*) FROM o_booking WHERE idempotency_key = $1",
                 f"telegram:{ALICE.ref}:order-1") == 1


def test_the_mode_is_part_of_the_request_a_key_names(schema):
    place(schema, simulated(client_key="order-2"))

    with pytest.raises(store.OrderError) as refused:
        create(schema, simulated(client_key="order-2", mode="SUPPLIER"))
    assert refused.value.code == "idempotency_conflict"


def test_a_repeat_at_another_rate_is_a_conflict(schema):
    place(schema, simulated(stay("100.00", source="75.50", rate="1.3243000000", currency="GBP"),
                            client_key="order-3"))

    with pytest.raises(store.OrderError) as refused:
        create(schema, simulated(stay("100.00", source="75.50", rate="1.3300000000", currency="GBP"),
                                 client_key="order-3"))
    assert refused.value.code == "idempotency_conflict"


def test_a_client_key_is_known_to_the_buyer_who_placed_it_only(schema):
    place(schema, simulated(client_key="order-4"))

    async def check(conn):
        return (await store.has_client_key(conn, ALICE, "order-4"),
                await store.has_client_key(conn, BOB, "order-4"))

    assert run(schema, check) == (True, False)


# ── Counting for limits ───────────────────────────────────────────────────────


def test_simulated_orders_are_counted_per_buyer_and_for_everyone(schema):
    async def totals(conn):
        return (await store.count_orders(conn, buyer=ALICE), await store.count_orders(conn, buyer=BOB),
                await store.count_orders(conn))

    alice_before, bob_before, all_before = run(schema, totals)
    place(schema, simulated(buyer=ALICE))
    place(schema, simulated(buyer=ALICE))
    place(schema, simulated(buyer=BOB))
    alice_after, bob_after, all_after = run(schema, totals)

    assert (alice_after - alice_before, bob_after - bob_before, all_after - all_before) == (2, 1, 3)


# ── Cancelling ────────────────────────────────────────────────────────────────


def test_cancelling_a_simulated_order_cancels_its_items(schema):
    booking = place(schema, simulated(stay("254.00"), stay("100.00")))

    async def cancel(conn):
        fresh = await store.get_booking(conn, booking.code, buyer=ALICE)
        return await store.cancel_booking(conn, fresh, actor="test")

    cancelled = run(schema, lambda conn: db.with_tx(conn, cancel))

    assert cancelled.status == "CANCELLED"
    assert {item.booking_status for item in cancelled.items} == {"CANCELLED"}
    assert count(schema, "SELECT count(*) FROM o_supplier_request WHERE item_id = ANY($1)",
                 [item.id for item in booking.items]) == 0


def test_a_cancelled_order_cannot_be_cancelled_again(schema):
    booking = place(schema, simulated())

    async def cancel_twice(conn):
        for _ in range(2):
            fresh = await store.get_booking(conn, booking.code, buyer=ALICE)
            await store.cancel_booking(conn, fresh, actor="test")

    with pytest.raises(store.OrderError) as refused:
        run(schema, lambda conn: db.with_tx(conn, cancel_twice))
    assert refused.value.code == "bad_move"


def test_an_order_a_supplier_holds_is_not_cancelled_here(schema):
    booking = create(schema, simulated(mode="SUPPLIER"))

    async def hold_then_cancel(conn):
        fresh = await store.get_booking(conn, booking.code, buyer=ALICE)
        await store.move_item(conn, fresh, fresh.items[0], fulfilment_status="SENT", actor="test")
        again = await store.get_booking(conn, booking.code, buyer=ALICE)
        await store.cancel_booking(conn, again, actor="test")

    with pytest.raises(store.OrderError) as refused:
        run(schema, lambda conn: db.with_tx(conn, hold_then_cancel))
    assert refused.value.code == "supplier_order_live"
