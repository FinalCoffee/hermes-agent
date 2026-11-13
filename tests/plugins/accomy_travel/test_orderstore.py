"""The order store against a real PostgreSQL 17, in a throwaway schema.

Needs the local test database; without it every test here is skipped:

    docker run -d --name accomy-pg-test -p 127.0.0.1:55433:5432 \
        -e POSTGRES_PASSWORD=accomy-test-only -e POSTGRES_DB=chatbot postgres:17
    docker exec accomy-pg-test psql -U postgres -d chatbot -c \
        "CREATE ROLE chatbot LOGIN PASSWORD 'accomy-test-only'; GRANT CREATE ON DATABASE chatbot TO chatbot"

The role is an ordinary one that may create schemas, like the application role on the real database.
"""

import asyncio
import dataclasses
import re
import secrets
import shutil
import socket
from argparse import Namespace
from datetime import date, datetime
from decimal import Decimal

import pytest

asyncpg = pytest.importorskip("asyncpg")

from plugins.accomy_travel.orderstore import cli, db, migrate, store

HOST, PORT = "127.0.0.1", 55433
DSN = f"postgresql://chatbot:accomy-test-only@{HOST}:{PORT}/chatbot"
ALICE = store.Buyer("telegram", "1001", "1001")
BOB = store.Buyer("telegram", "2002", "2002")


def _database_is_up() -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((HOST, PORT)) == 0


pytestmark = pytest.mark.skipif(not _database_is_up(), reason=f"no test PostgreSQL on {HOST}:{PORT}")


@pytest.fixture(scope="module")
def schema():
    name = f"acc_t_{secrets.token_hex(6)}"

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


def hotel(amount="420.00"):
    return store.NewItem(
        source="TMC", title="Park Hotel Tokyo / Deluxe King", sell_amount=Decimal(amount),
        detail=store.HotelStay(
            hotel_id="H123", hotel_name="Park Hotel Tokyo", room_id="R9", room_name="Deluxe King",
            check_in=date(2027, 3, 1), check_out=date(2027, 3, 4), adults=2),
        travellers=(store.Traveller("ADULT", given_name="Mei", family_name="Lin", email="mei@example.com"),))


def flight(amount="610.50"):
    leg = store.FlightSegment(
        journey=1, carrier="BR", flight_no="BR198", origin="TPE", destination="NRT",
        depart_local=datetime(2027, 3, 1, 8, 50), arrive_local=datetime(2027, 3, 1, 13, 15), cabin="ECONOMY")
    back = store.FlightSegment(
        journey=2, carrier="BR", flight_no="BR197", origin="NRT", destination="TPE",
        depart_local=datetime(2027, 3, 4, 14, 15), arrive_local=datetime(2027, 3, 4, 17, 5), cabin="ECONOMY")
    return store.NewItem(
        source="TMC", title="TPE to NRT return", sell_amount=Decimal(amount),
        detail=store.FlightItinerary(trip_kind="ROUND_TRIP", segments=(leg, back), adults=2))


def tickets(amount="300.00"):
    return store.NewItem(
        source="SEATUNIQUE", title="Final, Category 1", sell_amount=Decimal(amount), quantity=2,
        detail=store.EventTickets(event_id="E77", event_name="Final", listing_id="L5", venue_name="Stadium"))


def order(*items, buyer=ALICE, client_key=None, currency="USD"):
    return store.NewBooking(
        buyer=buyer, currency=currency, locale="zh-TW", items=items or (hotel(),),
        contact=store.Contact("Mei Lin", "mei@example.com", "+886900000000"), client_key=client_key)


def create(schema, new, **kwargs):
    return run(schema, lambda conn: db.with_tx(conn, lambda c: store.create_booking(c, new, actor="test", **kwargs)))


# ── Schema ────────────────────────────────────────────────────────────────────


def test_a_dry_run_leaves_the_database_untouched():
    name = f"acc_t_{secrets.token_hex(6)}"

    async def work(conn):
        would = await migrate.migrate(conn, commit=False, schema=name)
        exists = await conn.fetchval("SELECT to_regnamespace($1) IS NOT NULL", name)
        return [m.version for m in would], exists

    assert run(name, work) == ([m.version for m in migrate.load_migrations()], False)


def test_applied_migrations_are_not_applied_again(schema):
    async def work(conn):
        again = await migrate.migrate(conn, commit=True, schema=schema)
        return again, sorted(await migrate.applied_migrations(conn))

    assert run(schema, work) == ([], [m.version for m in migrate.load_migrations()])


def test_an_edited_migration_is_refused(schema, tmp_path):
    shutil.copytree(migrate.MIGRATIONS_DIR, tmp_path / "m")
    first = sorted((tmp_path / "m").glob("*.sql"))[0]
    first.write_text(first.read_text() + "\n-- edited after the fact\n")

    with pytest.raises(migrate.MigrationError, match="edited after it was applied"):
        run(schema, lambda conn: migrate.migrate(conn, commit=True, schema=schema, directory=tmp_path / "m"))


# ── Creating and reading ──────────────────────────────────────────────────────


def test_a_booking_keeps_what_was_ordered(schema):
    booking = create(schema, order(hotel(), flight(), tickets()))

    assert re.fullmatch(r"AC[0-9A-HJKMNP-TV-Z]{12}", booking.code)
    assert (booking.status, booking.currency, booking.total_amount) == ("DRAFT", "USD", Decimal("1330.50"))
    stay, itinerary, seats = booking.items
    assert (stay.kind, stay.detail["hotel_id"], stay.detail["check_out_date"]) == ("HOTEL", "H123", date(2027, 3, 4))
    assert [s["flight_no"] for s in itinerary.detail["segments"]] == ["BR198", "BR197"]
    assert (seats.kind, seats.quantity, seats.detail["listing_id"]) == ("EVENT_TICKET", 2, "L5")
    assert stay.travellers[0]["family_name"] == "Lin"
    assert {(i.booking_status, i.fulfilment_status) for i in booking.items} == {("PENDING", "NOT_SENT")}


def test_a_buyer_cannot_read_someone_elses_booking(schema):
    booking = create(schema, order())

    assert run(schema, lambda conn: store.get_booking(conn, booking.code, buyer=BOB)) is None
    assert booking.code not in {b["booking_code"] for b in run(schema, lambda conn: store.list_bookings(conn, BOB))}
    assert booking.code in {b["booking_code"] for b in run(schema, lambda conn: store.list_bookings(conn, ALICE))}


def test_a_repeated_request_returns_the_first_booking(schema):
    key = secrets.token_hex(8)
    first = create(schema, order(client_key=key))
    again = create(schema, order(client_key=key))

    assert (again.code, again.replayed, first.replayed) == (first.code, True, False)


def test_the_same_request_key_for_a_different_order_is_refused(schema):
    key = secrets.token_hex(8)
    create(schema, order(hotel("420.00"), client_key=key))

    with pytest.raises(store.OrderError) as refused:
        create(schema, order(hotel("999.00"), client_key=key))
    assert refused.value.code == "idempotency_conflict"


def test_the_same_request_key_for_other_items_at_the_same_price_is_refused(schema):
    key = secrets.token_hex(8)
    create(schema, order(hotel("300.00"), client_key=key))

    with pytest.raises(store.OrderError) as refused:
        create(schema, order(tickets("300.00"), client_key=key))
    assert refused.value.code == "idempotency_conflict"


@pytest.mark.parametrize("key", ["a:b", "x" * 65, "", "spa ce"])
def test_a_request_key_that_could_collide_or_overflow_is_refused(schema, key):
    with pytest.raises(store.OrderError) as refused:
        create(schema, order(client_key=key))
    assert refused.value.code == "bad_key"


def test_two_buyers_may_use_the_same_request_key(schema):
    key = secrets.token_hex(8)
    assert create(schema, order(client_key=key)).code != create(schema, order(buyer=BOB, client_key=key)).code


def test_concurrent_repeats_of_one_request_make_one_booking(schema):
    key = secrets.token_hex(8)

    async def both():
        async def one():
            conn = await db.connect(DSN, schema=schema)
            try:
                return await db.with_tx(conn, lambda c: store.create_booking(c, order(client_key=key), actor="test"))
            finally:
                await conn.close()

        return await asyncio.gather(one(), one())

    a, b = asyncio.run(both())
    assert a.code == b.code and sorted([a.replayed, b.replayed]) == [False, True]


@pytest.mark.parametrize("amount", [Decimal("10.005"), Decimal("-1.00"), 10.5, Decimal("1e16"), Decimal("NaN")])
def test_an_amount_the_column_would_round_is_refused(schema, amount):
    item = store.NewItem(source="TMC", title="x", sell_amount=amount, detail=hotel().detail)
    with pytest.raises(store.OrderError) as refused:
        create(schema, order(item))
    assert refused.value.code == "bad_amount"


def test_a_local_time_with_a_zone_is_refused(schema):
    aware = datetime.fromisoformat("2027-03-01T08:50:00+08:00")
    leg = store.FlightSegment(1, "BR", "BR198", "TPE", "NRT", aware, datetime(2027, 3, 1, 13, 15))
    item = store.NewItem(source="TMC", title="x", sell_amount=Decimal("1.00"),
                         detail=store.FlightItinerary("ONE_WAY", (leg,), adults=1))
    with pytest.raises(store.OrderError) as refused:
        create(schema, order(item))
    assert refused.value.code == "bad_time"


def test_a_buyers_bookings_are_listed_newest_first(schema):
    buyer = store.Buyer("telegram", str(secrets.randbelow(10**12)))
    codes = [create(schema, order(buyer=buyer)).code for _ in range(3)]

    listed = run(schema, lambda conn: store.list_bookings(conn, buyer, limit=2))
    assert [b["booking_code"] for b in listed] == codes[:0:-1]
    assert len(run(schema, lambda conn: store.list_bookings(conn, buyer, limit=-5))) == 1


# ── What the database itself refuses ──────────────────────────────────────────


def _sql(schema, statement, *args):
    return run(schema, lambda conn: db.with_tx(conn, lambda c: c.execute(statement, *args)))


def test_the_total_must_equal_the_items(schema):
    booking = create(schema, order())
    with pytest.raises(asyncpg.CheckViolationError, match="not the sum of its items"):
        _sql(schema, "UPDATE o_booking SET total_amount = total_amount + 1 WHERE id = $1", booking.id)


def test_an_item_added_without_raising_the_total_is_refused(schema):
    booking = create(schema, order())
    with pytest.raises(asyncpg.CheckViolationError, match="not the sum of its items"):
        _sql(schema, """
            WITH item AS (
                INSERT INTO o_booking_item (booking_id, item_seq, item_kind, source_code, title_snapshot,
                    quantity, currency, sell_amount) VALUES ($1, 2, 'EVENT_TICKET', 'SEATUNIQUE', 'x', 1, 'USD', 5)
                RETURNING id)
            INSERT INTO o_booking_item_event (item_id, event_id, event_name, listing_id)
            SELECT id, 'E', 'e', 'L' FROM item""", booking.id)


def test_a_booking_without_items_is_refused(schema):
    with pytest.raises(asyncpg.CheckViolationError, match="at least one item"):
        _sql(schema, """
            WITH customer AS (INSERT INTO o_customer (email) VALUES ('nobody@example.com') RETURNING id),
                 trip AS (INSERT INTO o_trip (trip_code, customer_id, title)
                          SELECT $2, id, 'Empty' FROM customer RETURNING id, customer_id)
            INSERT INTO o_booking (booking_code, buyer_channel, buyer_ref, locale_code, currency, customer_id, trip_id)
            SELECT $1, 'telegram', '1', 'en', 'USD', customer_id, id FROM trip""",
             store.new_booking_code(), store.new_trip_code())


def test_an_item_without_its_detail_is_refused(schema):
    booking = create(schema, order())
    with pytest.raises(asyncpg.CheckViolationError, match="has no detail row"):
        _sql(schema, """
            WITH item AS (
                INSERT INTO o_booking_item (booking_id, item_seq, item_kind, source_code, title_snapshot,
                    quantity, currency, sell_amount) VALUES ($1, 2, 'HOTEL', 'TMC', 'x', 1, 'USD', 0) RETURNING id)
            SELECT id FROM item""", booking.id)


@pytest.mark.parametrize("change", ["quantity = 5", "sell_amount = 1", "title_snapshot = 'other'", "item_seq = 9"])
def test_what_was_bought_cannot_be_changed(schema, change):
    booking = create(schema, order())
    with pytest.raises(asyncpg.CheckViolationError, match="immutable"):
        _sql(schema, f"UPDATE o_booking_item SET {change} WHERE booking_id = $1", booking.id)


@pytest.mark.parametrize("change", ["buyer_ref = '2002'", "currency = 'EUR'", "booking_code = 'AC000000000000'"])
def test_a_booking_cannot_change_hands_or_currency(schema, change):
    booking = create(schema, order())
    with pytest.raises((asyncpg.CheckViolationError, asyncpg.ForeignKeyViolationError)):
        _sql(schema, f"UPDATE o_booking SET {change} WHERE id = $1", booking.id)


def test_orders_and_their_history_cannot_be_removed(schema):
    booking = create(schema, order())
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
        _sql(schema, "UPDATE o_booking_item_hotel SET hotel_id = 'other' WHERE item_id = $1", booking.items[0].id)
    with pytest.raises(asyncpg.CheckViolationError, match="never deleted"):
        _sql(schema, "DELETE FROM o_booking WHERE id = $1", booking.id)
    with pytest.raises(asyncpg.CheckViolationError, match="never deleted"):
        _sql(schema, "DELETE FROM o_booking_item WHERE booking_id = $1", booking.id)
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
        _sql(schema, "UPDATE o_booking_event SET actor = 'x' WHERE booking_id = $1", booking.id)
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="never truncated"):
        _sql(schema, "TRUNCATE o_booking CASCADE")


def test_an_item_is_priced_in_its_bookings_currency(schema):
    booking = create(schema, order())
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        _sql(schema, """
            INSERT INTO o_booking_item (booking_id, item_seq, item_kind, source_code, title_snapshot, quantity,
                currency, sell_amount) VALUES ($1, 2, 'HOTEL', 'TMC', 'x', 1, 'EUR', 0)""", booking.id)


# ── Status moves ──────────────────────────────────────────────────────────────


def test_status_moves_follow_the_allowed_paths(schema):
    booking = create(schema, order())

    async def work(conn):
        await db.with_tx(conn, lambda c: store.move_booking(c, booking, "PENDING_PAYMENT", actor="test"))
        pending = await store.get_booking(conn, booking.code, buyer=ALICE)
        with pytest.raises(store.OrderError) as refused:
            await db.with_tx(conn, lambda c: store.move_booking(c, pending, "DRAFT", actor="test"))
        return pending.status, pending.version, refused.value.code

    assert run(schema, work) == ("PENDING_PAYMENT", booking.version + 1, "bad_move")


def test_a_move_from_a_stale_read_is_refused(schema):
    booking = create(schema, order())

    async def work(conn):
        await db.with_tx(conn, lambda c: store.move_booking(c, booking, "PENDING_PAYMENT", actor="first"))
        with pytest.raises(store.OrderError) as refused:
            await db.with_tx(conn, lambda c: store.move_booking(c, booking, "CANCELLED", actor="second"))
        return refused.value.code, (await store.get_booking(conn, booking.code, buyer=ALICE)).status

    assert run(schema, work) == ("stale", "PENDING_PAYMENT")


def test_an_item_move_from_a_stale_read_is_refused(schema):
    booking = create(schema, order())
    item = booking.items[0]

    async def work(conn):
        await db.with_tx(conn, lambda c: store.move_item(c, booking, item, fulfilment_status="SENT", actor="a"))
        with pytest.raises(store.OrderError) as refused:
            await db.with_tx(conn, lambda c: store.move_item(c, booking, item, booking_status="CONFIRMED", actor="b"))
        return refused.value.code

    assert run(schema, work) == "stale"


@pytest.mark.parametrize(("status", "to"), [("DRAFT", "COMPLETED"), ("DRAFT", "PARTIALLY_CANCELLED"), ("DRAFT", "DRAFT")])
def test_a_booking_cannot_skip_ahead(schema, status, to):
    booking = create(schema, order())
    with pytest.raises(store.OrderError) as refused:
        run(schema, lambda conn: db.with_tx(conn, lambda c: store.move_booking(c, booking, to, actor="t")))
    assert refused.value.code == "bad_move"


def test_an_item_cannot_be_cancelled_while_the_supplier_holds_its_order(schema):
    booking = create(schema, order())

    async def work(conn):
        item = booking.items[0]
        await db.with_tx(conn, lambda c: store.move_item(c, booking, item, fulfilment_status="SENT", actor="t"))
        sent = (await store.get_booking(conn, booking.code, buyer=ALICE)).items[0]
        with pytest.raises(store.OrderError) as refused:
            await db.with_tx(conn, lambda c: store.move_item(c, booking, sent, booking_status="CANCELLED", actor="t"))
        await db.with_tx(conn, lambda c: store.move_item(
            c, booking, sent, booking_status="CANCELLED", fulfilment_status="REJECTED", actor="t"))
        done = (await store.get_booking(conn, booking.code, buyer=ALICE)).items[0]
        return refused.value.code, done.booking_status, done.fulfilment_status

    assert run(schema, work) == ("supplier_order_live", "CANCELLED", "REJECTED")


def test_fulfilment_cannot_be_confirmed_before_anything_was_sent(schema):
    booking = create(schema, order())
    with pytest.raises(store.OrderError) as refused:
        run(schema, lambda conn: db.with_tx(conn, lambda c: store.move_item(
            c, booking, booking.items[0], fulfilment_status="CONFIRMED_BY_SUPPLIER", actor="t")))
    assert refused.value.code == "bad_move"


def test_an_item_moves_on_two_separate_axes(schema):
    booking = create(schema, order())

    async def work(conn):
        item = booking.items[0]
        await db.with_tx(conn, lambda c: store.move_item(c, booking, item, fulfilment_status="SENT", actor="test"))
        sent = (await store.get_booking(conn, booking.code, buyer=ALICE)).items[0]
        with pytest.raises(store.OrderError):
            await db.with_tx(conn, lambda c: store.move_item(c, booking, sent, booking_status="COMPLETED", actor="t"))
        return sent.booking_status, sent.fulfilment_status

    assert run(schema, work) == ("PENDING", "SENT")


# ── Supplier requests ─────────────────────────────────────────────────────────


def test_an_unanswered_supplier_write_blocks_the_next_attempt(schema):
    booking = create(schema, order())
    item = booking.items[0]

    async def work(conn):
        first = await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PLACE", actor="t"))
        with pytest.raises(store.OrderError) as while_new:
            await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PLACE", actor="t"))
        await db.with_tx(conn, lambda c: store.close_supplier_request(
            c, booking, first, "UNKNOWN", result_code="timeout", actor="t"))
        with pytest.raises(store.OrderError) as while_unknown:
            await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PLACE", actor="t"))
        return first.client_request_key, while_new.value.code, while_unknown.value.code

    assert run(schema, work) == (f"ac:item:{item.id}:place:1", "request_pending", "request_pending")


def test_a_failed_attempt_allows_a_new_one_with_its_own_key(schema):
    booking = create(schema, order())
    item = booking.items[0]

    async def work(conn):
        first = await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PLACE", actor="t"))
        await db.with_tx(conn, lambda c: store.close_supplier_request(
            c, booking, first, "FAILED", result_code="rejected", actor="t"))
        second = await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PLACE", actor="t"))
        with pytest.raises(store.OrderError) as settled_twice:
            await db.with_tx(conn, lambda c: store.close_supplier_request(
                c, booking, first, "PLACED", result_code="ok", actor="t"))
        return second.attempt_no, second.client_request_key, settled_twice.value.code

    assert run(schema, work) == (2, f"ac:item:{item.id}:place:2", "stale")


def test_a_placed_order_puts_the_suppliers_numbers_on_the_item(schema):
    booking = create(schema, order())
    item = booking.items[0]
    pay_by = datetime.fromisoformat("2027-02-01T10:00:00+00:00")

    async def work(conn):
        request = await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PLACE", actor="t"))
        await db.with_tx(conn, lambda c: store.close_supplier_request(
            c, booking, request, "PLACED", result_code="ok", supplier_order_no="INV20270001",
            supplier_confirm_no="CONF88", pay_by_at=pay_by, response_sha256="ab" * 32, actor="t"))
        return (await store.get_booking(conn, booking.code, buyer=ALICE)).items[0]

    placed = run(schema, work)
    assert (placed.supplier_order_no, placed.supplier_confirm_no, placed.pay_by_at) == ("INV20270001", "CONF88", pay_by)


def test_an_item_is_placed_only_once(schema):
    booking = create(schema, order())
    item = booking.items[0]

    async def work(conn):
        request = await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PLACE", actor="t"))
        await db.with_tx(conn, lambda c: store.close_supplier_request(
            c, booking, request, "PLACED", result_code="ok", supplier_order_no="INV1", actor="t"))
        with pytest.raises(store.OrderError) as refused:
            await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PLACE", actor="t"))
        return refused.value.code

    assert run(schema, work) == "already_placed"
    with pytest.raises(asyncpg.UniqueViolationError, match="o_supplier_request_uk_placed"):
        _sql(schema, """
            INSERT INTO o_supplier_request (item_id, source_code, request_kind, attempt_no, client_request_key, state)
            VALUES ($1, 'TMC', 'PLACE', 9, $2, 'PLACED')""", item.id, secrets.token_hex(8))


def test_a_second_live_attempt_is_refused_by_the_database_too(schema):
    booking = create(schema, order())
    item = booking.items[0]
    run(schema, lambda conn: db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PLACE", actor="t")))
    with pytest.raises(asyncpg.UniqueViolationError, match="o_supplier_request_uk_live"):
        _sql(schema, """
            INSERT INTO o_supplier_request (item_id, source_code, request_kind, attempt_no, client_request_key)
            VALUES ($1, 'TMC', 'PLACE', 9, $2)""", item.id, secrets.token_hex(8))


def test_paying_needs_a_placed_order_and_each_kind_counts_its_own_attempts(schema):
    booking = create(schema, order())
    item = booking.items[0]

    async def work(conn):
        with pytest.raises(store.OrderError) as too_early:
            await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PAY", actor="t"))
        place = await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PLACE", actor="t"))
        await db.with_tx(conn, lambda c: store.close_supplier_request(
            c, booking, place, "PLACED", result_code="ok", supplier_order_no="INV1", actor="t"))
        pay = await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PAY", actor="t"))
        return too_early.value.code, pay.attempt_no, pay.client_request_key

    assert run(schema, work) == ("not_placed", 1, f"ac:item:{item.id}:pay:1")


def test_a_supplier_write_cannot_be_logged_against_another_booking(schema):
    mine, theirs = create(schema, order()), create(schema, order(buyer=BOB))

    with pytest.raises(store.OrderError) as refused:
        run(schema, lambda conn: db.with_tx(conn, lambda c: store.open_supplier_request(
            c, mine, theirs.items[0], "PLACE", actor="t")))
    assert refused.value.code == "not_this_booking"
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        _sql(schema, """
            INSERT INTO o_supplier_request (item_id, source_code, request_kind, attempt_no, client_request_key)
            VALUES ($1, 'SEATUNIQUE', 'PLACE', 1, $2)""", mine.items[0].id, secrets.token_hex(8))


@pytest.mark.parametrize(("extra", "code"), [
    ({"result_code": "Hotel said: no rooms left!"}, "bad_result"),
    ({"result_code": "ok", "pay_by_at": datetime(2027, 2, 1, 10, 0)}, "bad_time"),
])
def test_a_result_the_log_cannot_hold_is_refused_before_it_is_written(schema, extra, code):
    booking = create(schema, order())

    async def work(conn):
        request = await db.with_tx(conn, lambda c: store.open_supplier_request(
            c, booking, booking.items[0], "PLACE", actor="t"))
        with pytest.raises(store.OrderError) as refused:
            await db.with_tx(conn, lambda c: store.close_supplier_request(
                c, booking, request, "PLACED", actor="t", **extra))
        return refused.value.code

    assert run(schema, work) == code


def test_two_workers_cannot_both_open_the_same_supplier_write(schema):
    booking = create(schema, order())
    item = booking.items[0]

    async def both():
        async def one():
            conn = await db.connect(DSN, schema=schema)
            try:
                await db.with_tx(conn, lambda c: store.open_supplier_request(c, booking, item, "PLACE", actor="t"))
                return "opened"
            except store.OrderError as refused:
                return refused.code
            finally:
                await conn.close()

        return await asyncio.gather(one(), one())

    assert sorted(asyncio.run(both())) == ["opened", "request_pending"]


# ── Personal data ─────────────────────────────────────────────────────────────


def test_erasing_removes_the_people_and_keeps_the_order(schema):
    booking = create(schema, order())

    async def work(conn):
        await db.with_tx(conn, lambda c: store.erase_personal_data(c, booking, actor="test"))
        kept = await conn.fetchrow("SELECT * FROM o_booking WHERE id = $1", booking.id)
        traveller = await conn.fetchrow(
            "SELECT * FROM o_booking_item_traveller WHERE item_id = $1", booking.items[0].id)
        return kept, traveller

    kept, traveller = run(schema, work)
    assert (kept["contact_name"], kept["contact_email"], kept["contact_phone"]) == (None, None, None)
    assert (traveller["given_name"], traveller["family_name"], traveller["email"]) == (None, None, None)
    assert traveller["erased_at"] is not None
    assert (kept["booking_code"], kept["total_amount"], kept["status"]) == (booking.code, booking.total_amount, "DRAFT")
    with pytest.raises(asyncpg.CheckViolationError):
        _sql(schema, "UPDATE o_booking SET contact_email = 'back@example.com' WHERE id = $1", booking.id)


def test_erasing_forgets_the_buyer_and_is_final(schema):
    buyer = store.Buyer("telegram", "77123456", "-10077123456")
    key = secrets.token_hex(8)
    booking = create(schema, order(buyer=buyer, client_key=key))

    async def work(conn):
        await db.with_tx(conn, lambda c: store.erase_personal_data(c, booking, actor="test"))
        await db.with_tx(conn, lambda c: store.erase_personal_data(c, booking, actor="test"))
        row = await conn.fetchrow("SELECT * FROM o_booking WHERE id = $1", booking.id)
        events = await conn.fetchval(
            "SELECT count(*) FROM o_booking_event WHERE booking_id = $1 AND event_kind = 'ERASED'", booking.id)
        return dict(row), events, await store.get_booking(conn, booking.code, buyer=buyer)

    row, erased_events, visible_to_buyer = run(schema, work)
    assert "77123456" not in repr(row) and (row["buyer_ref"], row["chat_ref"]) == (f"erased:{booking.code}", None)
    assert (row["idempotency_key"], row["request_sha256"], erased_events, visible_to_buyer) == (None, None, 1, None)
    for undo in ("erased_at = NULL", "buyer_ref = '77123456'"):
        with pytest.raises(asyncpg.CheckViolationError):
            _sql(schema, f"UPDATE o_booking SET {undo} WHERE id = $1", booking.id)
    with pytest.raises(asyncpg.CheckViolationError, match="erasure is final"):
        _sql(schema, "UPDATE o_booking_item_traveller SET erased_at = NULL, given_name = 'Mei' WHERE item_id = $1",
             booking.items[0].id)
    with pytest.raises(asyncpg.CheckViolationError, match="takes no traveller"):
        _sql(schema, "INSERT INTO o_booking_item_traveller (item_id, traveller_seq, traveller_kind, given_name) "
                     "VALUES ($1, 2, 'ADULT', 'New')", booking.items[0].id)
    with pytest.raises(store.OrderError) as refused:
        run(schema, lambda conn: db.with_tx(conn, lambda c: store.open_supplier_request(
            c, booking, booking.items[0], "PLACE", actor="t")))
    assert refused.value.code == "erased"


def test_a_booking_cannot_be_erased_while_a_traveller_is_not(schema):
    booking = create(schema, order())
    with pytest.raises(asyncpg.CheckViolationError, match="a traveller is not"):
        _sql(schema, """
            UPDATE o_booking SET contact_name = NULL, contact_email = NULL, contact_phone = NULL, chat_ref = NULL,
                buyer_ref = 'erased:' || booking_code, erased_at = clock_timestamp() WHERE id = $1""", booking.id)


def test_a_traveller_stays_on_its_item(schema):
    mine, theirs = create(schema, order()), create(schema, order(buyer=BOB))
    with pytest.raises(asyncpg.CheckViolationError, match="stays on its item"):
        _sql(schema, "UPDATE o_booking_item_traveller SET item_id = $1, traveller_seq = 2 WHERE item_id = $2",
             theirs.items[0].id, mine.items[0].id)


# ── Transactions ──────────────────────────────────────────────────────────────


def test_a_transaction_that_lost_a_race_runs_again(schema):
    calls = []

    async def work(conn):
        async def body(c):
            calls.append(1)
            if len(calls) == 1:
                raise asyncpg.SerializationError("could not serialize access")
            return await c.fetchval("SELECT 42")

        return await db.with_tx(conn, body)

    assert (run(schema, work), len(calls)) == (42, 2)


def test_a_statement_timeout_is_not_run_again(schema):
    calls = []

    async def work(conn):
        async def body(c):
            calls.append(1)
            await c.execute("SELECT pg_sleep(2)")

        await db.with_tx(conn, body, statement_timeout_ms=100)

    with pytest.raises(asyncpg.QueryCanceledError):
        run(schema, work)
    assert len(calls) == 1


# ── Command line ──────────────────────────────────────────────────────────────


def test_the_cli_dry_run_reports_and_changes_nothing(monkeypatch, capsys):
    monkeypatch.setenv(db.DSN_ENV, DSN)
    monkeypatch.setattr(db, "SCHEMA", f"acc_t_{secrets.token_hex(6)}")

    assert cli.run(Namespace(action="migrate", commit=False)) == 0
    out = capsys.readouterr().out
    assert "would apply  0001_" in out and "rolled back" in out

    async def schema_exists():
        conn = await asyncpg.connect(DSN)
        try:
            return await conn.fetchval("SELECT to_regnamespace($1) IS NOT NULL", db.SCHEMA)
        finally:
            await conn.close()

    assert asyncio.run(schema_exists()) is False


# ── Customers and trips ───────────────────────────────────────────────────────


def _person():
    return store.Buyer("telegram", str(secrets.randbelow(10**12)))


def test_an_order_starts_a_trip_and_a_later_order_can_join_it(schema):
    buyer = _person()
    first = create(schema, order(flight(), buyer=buyer, client_key=None))
    second = create(schema, dataclasses.replace(order(hotel(), buyer=buyer), trip_code=first.trip.code))
    other = create(schema, order(tickets(), buyer=buyer))

    trips = run(schema, lambda conn: store.list_trips(conn, buyer))
    assert [t["trip_code"] for t in trips] == [other.trip.code, first.trip.code]
    joined = trips[1]
    assert [b["booking_code"] for b in joined["bookings"]] == [first.code, second.code]
    assert [b["kinds"] for b in joined["bookings"]] == [["FLIGHT"], ["HOTEL"]]
    assert (joined["starts_on"], joined["ends_on"], joined["status"]) == (date(2027, 3, 1), date(2027, 3, 4), "ACTIVE")
    assert (second.trip.code, second.trip.title) == (first.trip.code, first.trip.title)


def test_a_trip_takes_its_name_from_the_order_unless_one_is_given(schema):
    buyer = _person()
    unnamed = create(schema, order(hotel(), buyer=buyer))
    named = create(schema, dataclasses.replace(order(hotel(), buyer=buyer), trip_title="  東京  賞櫻 "))

    assert (unnamed.trip.title, named.trip.title) == ("Park Hotel Tokyo / Deluxe King", "東京 賞櫻")
    assert re.fullmatch(r"TR[0-9A-HJKMNP-TV-Z]{12}", named.trip.code)


def test_an_order_cannot_join_someone_elses_trip_or_a_closed_one(schema):
    mine, theirs = _person(), _person()
    their_trip = create(schema, order(buyer=theirs)).trip.code
    my_trip = create(schema, order(buyer=mine)).trip.code
    _sql(schema, "UPDATE o_trip SET status = 'COMPLETED' WHERE trip_code = $1", my_trip)

    for code in (their_trip, my_trip, "TR000000000000"):
        with pytest.raises(store.OrderError) as refused:
            create(schema, dataclasses.replace(order(buyer=mine), trip_code=code))
        assert refused.value.code == "no_trip"
    assert [len(t["bookings"]) for t in run(schema, lambda conn: store.list_trips(conn, theirs))] == [1]


def test_a_repeated_request_does_not_start_a_second_trip(schema):
    buyer = _person()
    key = secrets.token_hex(8)
    first = create(schema, order(buyer=buyer, client_key=key))
    again = create(schema, order(buyer=buyer, client_key=key))

    assert (again.trip.code, again.replayed) == (first.trip.code, True)
    assert len(run(schema, lambda conn: store.list_trips(conn, buyer))) == 1


def test_one_telegram_account_is_one_customer_who_keeps_the_newest_contact(schema):
    buyer = _person()
    create(schema, order(buyer=buyer))
    create(schema, dataclasses.replace(order(buyer=buyer), contact=store.Contact("Mei L.", "NEW@Example.com", None)))

    by_account = run(schema, lambda conn: store.find_customers(conn, telegram_user_id=buyer.ref))
    by_email = run(schema, lambda conn: store.find_customers(conn, email="new@example.COM"))
    assert len(by_account) == 1 and [c["id"] for c in by_email] == [by_account[0]["id"]]
    customer = by_account[0]
    assert (customer["display_name"], customer["email"], customer["phone"]) == ("Mei L.", "NEW@Example.com", "+886900000000")
    assert sum(len(t["bookings"]) for t in customer["trips"]) == 2
    assert run(schema, lambda conn: store.find_customers(conn, email="old@example.com")) == []


def test_a_booking_belongs_to_a_trip_of_its_own_customer(schema):
    mine, theirs = create(schema, order(buyer=_person())), create(schema, order(buyer=_person()))
    with pytest.raises(asyncpg.CheckViolationError, match="immutable"):
        _sql(schema, "UPDATE o_booking SET trip_id = (SELECT trip_id FROM o_booking WHERE id = $2) WHERE id = $1",
             mine.id, theirs.id)
    with pytest.raises(asyncpg.CheckViolationError, match="immutable"):
        _sql(schema, "UPDATE o_trip SET customer_id = (SELECT customer_id FROM o_booking WHERE id = $2) "
                     "WHERE id = (SELECT trip_id FROM o_booking WHERE id = $1)", mine.id, theirs.id)


def test_erasing_a_customer_erases_them_everywhere_and_keeps_the_orders(schema):
    buyer = _person()
    booking = create(schema, dataclasses.replace(order(buyer=buyer), trip_title="Mei's birthday trip"))

    async def work(conn):
        customer = (await store.find_customers(conn, telegram_user_id=buyer.ref))[0]
        await db.with_tx(conn, lambda c: store.erase_customer(c, customer["id"], actor="test"))
        row = await conn.fetchrow("SELECT * FROM o_customer WHERE id = $1", customer["id"])
        kept = await conn.fetchrow(
            "SELECT b.total_amount, b.buyer_ref, t.title FROM o_booking b JOIN o_trip t ON t.id = b.trip_id "
            "WHERE b.id = $1", booking.id)
        return dict(row), dict(kept), await store.find_customers(conn, telegram_user_id=buyer.ref)

    row, kept, found_again = run(schema, work)
    assert (row["telegram_user_id"], row["email"], row["display_name"], row["phone"]) == (None, None, None, None)
    assert row["erased_at"] is not None and found_again == []
    assert kept == {"total_amount": booking.total_amount, "buyer_ref": f"erased:{booking.code}", "title": "Trip"}


def test_a_customer_cannot_be_erased_while_a_booking_still_names_them(schema):
    buyer = _person()
    create(schema, order(buyer=buyer))
    with pytest.raises(asyncpg.CheckViolationError, match="one of their bookings is not"):
        _sql(schema, """
            UPDATE o_customer SET telegram_user_id = NULL, email = NULL, display_name = NULL, phone = NULL,
                erased_at = clock_timestamp() WHERE telegram_user_id = $1""", buyer.ref)


def test_two_identical_requests_at_once_start_one_trip(schema):
    buyer = _person()
    key = secrets.token_hex(8)

    async def both():
        async def one():
            conn = await db.connect(DSN, schema=schema)
            try:
                return await db.with_tx(conn, lambda c: store.create_booking(c, order(buyer=buyer, client_key=key), actor="t"))
            finally:
                await conn.close()

        return await asyncio.gather(one(), one())

    a, b = asyncio.run(both())
    trips = run(schema, lambda conn: conn.fetchval(
        "SELECT count(*) FROM o_trip t JOIN o_customer c ON c.id = t.customer_id WHERE c.telegram_user_id = $1",
        buyer.ref))
    assert (a.code, a.trip.code, trips) == (b.code, b.trip.code, 1)


def test_a_customer_known_only_by_email_is_one_customer(schema):
    email = f"{secrets.token_hex(6)}@Example.com"
    web = store.Buyer("web", "session-1")

    async def both():
        async def one(contact):
            conn = await db.connect(DSN, schema=schema)
            try:
                new = dataclasses.replace(order(buyer=web), contact=contact)
                return await db.with_tx(conn, lambda c: store.create_booking(c, new, actor="t"))
            finally:
                await conn.close()

        return await asyncio.gather(one(store.Contact("A", email, None)), one(store.Contact("B", email.lower(), "+1 555 0100")))

    asyncio.run(both())
    found = run(schema, lambda conn: store.find_customers(conn, email=email.upper()))
    assert len(found) == 1 and found[0]["phone"] == "+1 555 0100" and len(found[0]["trips"]) == 2


def test_an_order_outside_telegram_needs_an_email_to_find_its_customer_by(schema):
    new = dataclasses.replace(order(buyer=store.Buyer("web", "session-2")), contact=store.Contact("A", None, "+1 555 0100"))
    with pytest.raises(store.OrderError) as refused:
        create(schema, new)
    assert refused.value.code == "no_customer"


def test_a_rate_with_more_decimals_than_the_column_keeps_is_refused(schema):
    item = dataclasses.replace(tickets("12.00"), conversion=store.FxConversion(
        Decimal("9.00"), "GBP", Decimal("1.33333333333333"), datetime.fromisoformat("2026-11-13T00:00:00+00:00"), "test"))
    with pytest.raises(store.OrderError) as refused:
        create(schema, order(item))
    assert refused.value.code == "bad_rate"
