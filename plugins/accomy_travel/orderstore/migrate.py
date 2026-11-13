"""Apply the order store's schema: numbered, forward-only SQL files recorded in ``c_schema_migration``.

A file is ``NNNN_YYYY-MM-DD_<kebab-name>.sql``. Files are applied in number order, all pending ones in
a single transaction, and each is recorded with the SHA-256 of its text. An applied file is never
edited: a correction is a new file. Without ``commit`` the transaction is rolled back, which is the
dry run.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILE_NAME = re.compile(r"(\d{4})_\d{4}-\d{2}-\d{2}_[a-z0-9]+(?:-[a-z0-9]+)*\.sql")
# Dollar-quoted bodies, quoted strings and comments: where BEGIN and END are not transaction control.
_NOT_STATEMENTS = re.compile(r"\$(\w*)\$.*?\$\1\$|'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.DOTALL)
_TRANSACTION_CONTROL = re.compile(
    r"(?:^|;)\s*(BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE|PREPARE\s+TRANSACTION)\b",
    re.IGNORECASE)


class MigrationError(Exception):
    """The files and the database disagree; nothing was applied."""


class _DryRun(Exception):
    """Unwinds the transaction of a dry run."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_NAME.fullmatch(path.name)
        if not match:
            raise MigrationError(f"{path.name}: expected NNNN_YYYY-MM-DD_<kebab-name>.sql")
        sql = path.read_text(encoding="utf-8-sig")
        if control := _TRANSACTION_CONTROL.search(_NOT_STATEMENTS.sub(" ", sql)):
            # The runner owns the transaction: a COMMIT inside a file would keep half of it.
            raise MigrationError(f"{path.name}: a migration must not contain {control[1].upper()}")
        found.append(Migration(int(match[1]), path.stem, sql, hashlib.sha256(sql.encode()).hexdigest()))
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migration numbers must run 1..{len(found)} without gaps: {versions}")
    return found


async def applied_migrations(conn: Any) -> dict[int, tuple[str, str]]:
    """``{version: (name, checksum)}`` from the ledger; empty before the first migration."""
    if await conn.fetchval("SELECT to_regclass('c_schema_migration')") is None:
        return {}
    rows = await conn.fetch("SELECT version, name, checksum FROM c_schema_migration ORDER BY version")
    return {row["version"]: (row["name"], row["checksum"]) for row in rows}


def pending_migrations(migrations: list[Migration], applied: dict[int, tuple[str, str]]) -> list[Migration]:
    by_version = {m.version: m for m in migrations}
    for version, (name, checksum) in applied.items():
        known = by_version.get(version)
        if known is None:
            raise MigrationError(f"the database has migration {version} ({name}), which this code does not know")
        if known.checksum != checksum:
            raise MigrationError(f"{known.name} was edited after it was applied: write a new migration instead")
    return [m for m in migrations if m.version not in applied]


async def migrate(
    conn: Any, *, commit: bool, schema: str | None = None, directory: Path = MIGRATIONS_DIR,
) -> list[Migration]:
    """Apply every pending migration in one transaction and return them; roll back unless *commit*."""
    schema = db.check_schema_name(schema or db.SCHEMA)
    migrations = load_migrations(directory)
    ran: list[Migration] = []
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL lock_timeout = '5s'; SET LOCAL statement_timeout = '60s'")
            # One runner at a time per schema; a second one waits here, then finds nothing pending.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"accomy.orderstore.migrate:{schema}")
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"; SET LOCAL search_path TO "{schema}"')
            for migration in pending_migrations(migrations, await applied_migrations(conn)):
                await conn.execute(migration.sql)
                await conn.execute(
                    "INSERT INTO c_schema_migration (version, name, checksum) VALUES ($1, $2, $3)",
                    migration.version, migration.name, migration.checksum)
                ran.append(migration)
            if not commit:
                raise _DryRun
    except _DryRun:
        pass
    return ran
