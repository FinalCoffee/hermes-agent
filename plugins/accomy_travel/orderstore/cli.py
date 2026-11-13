"""``hermes accomy-orders``: apply and inspect the order store's schema."""

from __future__ import annotations

import asyncio
import sys

from . import db, migrate, store

COMMAND = "accomy-orders"
HELP = "Apply or inspect the Accomy order store's database schema"


def setup(parser) -> None:
    actions = parser.add_subparsers(dest="action", required=True)
    apply = actions.add_parser(
        "migrate", help="apply pending migrations (a dry run that changes nothing, unless --commit)")
    apply.add_argument("--commit", action="store_true", help="keep the changes instead of rolling them back")
    actions.add_parser("status", help="show which migrations the database has")
    customer = actions.add_parser(
        "customer", help="find a customer by Telegram account id or email, with their trips and orders")
    who = customer.add_mutually_exclusive_group(required=True)
    who.add_argument("--telegram", metavar="ID", help="the customer's Telegram account id")
    who.add_argument("--email", metavar="ADDRESS", help="a contact email (several customers may share one)")


def run(args) -> int:
    try:
        return asyncio.run(_run(args))
    except (db.StoreUnavailable, migrate.MigrationError) as exc:
        print(f"accomy-orders: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if type(exc).__module__.partition(".")[0] != "asyncpg":
            raise
        # A table that is not there yet, a missing privilege: say which, without the statement or its values.
        print(f"accomy-orders: the database refused ({type(exc).__name__}). Is the schema migrated? "
              "Check with: accomy-orders status", file=sys.stderr)
        return 1


async def _run(args) -> int:
    conn = await db.connect()
    try:
        if args.action == "status":
            applied = await migrate.applied_migrations(conn)
            pending = migrate.pending_migrations(migrate.load_migrations(), applied)
            server = await conn.fetchval("SHOW server_version")
            print(f"PostgreSQL {server}, schema {db.SCHEMA}")
            for version, (name, _) in applied.items():
                print(f"  applied  {name}")
            for migration in pending:
                print(f"  pending  {migration.name}")
            return 0
        if args.action == "customer":
            return _show_customers(await store.find_customers(conn, telegram_user_id=args.telegram, email=args.email))
        ran = await migrate.migrate(conn, commit=args.commit)
        for migration in ran:
            print(f"  {'applied' if args.commit else 'would apply'}  {migration.name}")
        if not ran:
            print("Nothing to apply.")
        elif not args.commit:
            print("Dry run: rolled back. Run again with --commit to keep it.")
        return 0
    finally:
        await conn.close()


def _show_customers(customers: list[dict]) -> int:
    """Print each customer with their trips (master) and the orders under each (detail)."""
    if not customers:
        print("No customer found.")
        return 1
    for customer in customers:
        print(f"Customer {customer['id']}: {customer['display_name'] or '-'}  telegram {customer['telegram_user_id'] or '-'}"
              f"  email {customer['email'] or '-'}  phone {customer['phone'] or '-'}")
        for trip in customer["trips"]:
            dates = f"{trip['starts_on'] or '?'} to {trip['ends_on'] or '?'}"
            print(f"  Trip {trip['trip_code']}  {trip['status']}  {dates}  {trip['title']}")
            for booking in trip["bookings"]:
                print(f"    {booking['booking_code']}  {booking['status']:<10} {booking['currency']} "
                      f"{booking['total_amount']:>10.2f}  {booking['created_dt']:%Y-%m-%d %H:%M}  {booking['summary']}")
    return 0
