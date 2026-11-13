"""``hermes accomy-orders``: apply and inspect the order store's schema."""

from __future__ import annotations

import asyncio
import sys

from . import db, migrate

COMMAND = "accomy-orders"
HELP = "Apply or inspect the Accomy order store's database schema"


def setup(parser) -> None:
    actions = parser.add_subparsers(dest="action", required=True)
    apply = actions.add_parser(
        "migrate", help="apply pending migrations (a dry run that changes nothing, unless --commit)")
    apply.add_argument("--commit", action="store_true", help="keep the changes instead of rolling them back")
    actions.add_parser("status", help="show which migrations the database has")


def run(args) -> int:
    try:
        return asyncio.run(_run(args))
    except (db.StoreUnavailable, migrate.MigrationError) as exc:
        print(f"accomy-orders: {exc}", file=sys.stderr)
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
