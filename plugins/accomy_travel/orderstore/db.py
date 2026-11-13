"""Connections and transactions for the order store (PostgreSQL through asyncpg)."""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import parse_qs, urlsplit

from .._common import secret

DSN_ENV = "ACCOMY_DATABASE_URL"
SCHEMA = "accomy"
# Reruns per SQLSTATE: lock_not_available, deadlock_detected, serialization_failure. A statement
# timeout (57014) is never rerun: the same statement would time out again.
_RERUNS = {"55P03": 3, "40P01": 1, "40001": 3}

T = TypeVar("T")


class StoreUnavailable(Exception):
    """The store cannot be reached from this process: no driver or no connection string."""


def check_schema_name(schema: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError(f"not a schema name: {schema!r}")
    return schema


def _require_verified_tls(dsn: str) -> None:
    """Refuse a remote server unless the string asks for a verified certificate.

    The driver's default is opportunistic: it would fall back to plain text, and to an unverified
    server, without a word. Loopback is exempt (the local test database has no TLS).
    """
    url = urlsplit(dsn)
    if url.hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    if parse_qs(url.query).get("sslmode", [""])[-1] not in {"verify-full", "verify-ca"}:
        raise StoreUnavailable(
            f"{DSN_ENV} must end with ?sslmode=verify-full for a server that is not on this machine.")


async def connect(dsn: str | None = None, *, schema: str | None = None) -> Any:
    """Open one connection whose unqualified names resolve in *schema* (default ``SCHEMA``).

    The connection string is read per call, from the profile's secrets, so one process can serve
    several profiles. The CA comes from ``sslrootcert`` in the string or from ``PGSSLROOTCERT``.
    """
    try:
        import asyncpg
    except ImportError as exc:
        raise StoreUnavailable(
            "The order store needs the asyncpg driver: install the 'postgres' extra "
            "(hermes pm install --extra postgres).") from exc
    dsn = dsn or secret(DSN_ENV)
    if not dsn:
        raise StoreUnavailable(f"{DSN_ENV} is not set in the profile's .env.")
    _require_verified_tls(dsn)
    try:
        return await asyncpg.connect(
            dsn, timeout=10,
            server_settings={"search_path": check_schema_name(schema or SCHEMA), "application_name": "accomy-bot"})
    except (OSError, asyncpg.PostgresError) as exc:
        # Unreachable host, failed certificate check, wrong password. The text names no credential.
        raise StoreUnavailable(f"cannot connect to the order database: {type(exc).__name__}: {exc}") from exc


async def with_tx(
    conn: Any, fn: Callable[[Any], Awaitable[T]], *, statement_timeout_ms: int = 5000,
) -> T:
    """Run ``fn(conn)`` in one READ COMMITTED transaction and return its result.

    A lock wait gives up after 2 seconds and a statement after *statement_timeout_ms*. When the
    transaction loses to a lock, a deadlock or a serialization failure, the whole of *fn* runs
    again, so *fn* must not call a supplier or send anything: do that between transactions.
    """
    import asyncpg

    reruns: dict[str, int] = {}
    while True:
        try:
            async with conn.transaction():
                await conn.execute(
                    f"SET LOCAL lock_timeout = '2s'; SET LOCAL statement_timeout = {int(statement_timeout_ms)}")
                return await fn(conn)
        except asyncpg.PostgresError as exc:
            code = exc.sqlstate or ""
            reruns[code] = reruns.get(code, 0) + 1
            if reruns[code] > _RERUNS.get(code, 0):
                raise
            await asyncio.sleep(random.uniform(0.005, 0.05))
