"""Deterministic, forward-only SQL migrations.

* Files live in ``iris_pilot/migrations`` and are named ``NNNN_description.sql``.
  Versions must be contiguous from 0001, so ordering never depends on the
  file system.
* Every applied file is recorded with a SHA-256 of its LF-normalised text.
  Editing an applied migration, or running code that is older than the
  database, stops the process instead of guessing.
* Concurrent runners are serialised with a PostgreSQL advisory lock and each
  migration commits atomically together with its ledger row.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources

import psycopg

from .log import fields

log = logging.getLogger(__name__)

LEDGER_TABLE = "public.schema_migration"
# Arbitrary but fixed; only needs to be unique among advisory locks used here.
ADVISORY_LOCK_KEY = 72_201_909_001
_FILENAME_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    pass


class SchemaBehind(MigrationError):
    """The schema is valid but has migrations still to apply."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def checksum(sql: str) -> str:
    normalised = sql.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def discover(files: Iterable[tuple[str, str]] | None = None) -> list[Migration]:
    """Return migrations ordered by version; ``files`` is (filename, text) pairs."""
    if files is None:
        package = resources.files("iris_pilot") / "migrations"
        files = [(p.name, p.read_text(encoding="utf-8")) for p in package.iterdir() if p.name.endswith(".sql")]

    found: dict[int, Migration] = {}
    for filename, text in files:
        match = _FILENAME_RE.fullmatch(filename)
        if not match:
            raise MigrationError(f"migration file {filename!r} does not match NNNN_lower_snake_name.sql")
        version = int(match["version"])
        if version in found:
            raise MigrationError(f"duplicate migration version {version:04d}")
        found[version] = Migration(version, match["name"], text, checksum(text))

    ordered = [found[v] for v in sorted(found)]
    expected = list(range(1, len(ordered) + 1))
    if [m.version for m in ordered] != expected:
        raise MigrationError(f"migration versions must be contiguous from 0001, found {[m.version for m in ordered]}")
    return ordered


def applied_migrations(conn: psycopg.Connection) -> dict[int, tuple[str, str]]:
    if conn.execute("SELECT to_regclass(%s)", (LEDGER_TABLE,)).fetchone()[0] is None:
        return {}
    rows = conn.execute(f"SELECT version, name, checksum FROM {LEDGER_TABLE} ORDER BY version").fetchall()
    return {version: (name, digest) for version, name, digest in rows}


def pending(conn: psycopg.Connection, migrations: list[Migration]) -> list[Migration]:
    """Validate the ledger against the code and return what is still to apply."""
    applied = applied_migrations(conn)
    known = {m.version: m for m in migrations}

    ahead = sorted(set(applied) - set(known))
    if ahead:
        raise MigrationError(
            f"database has migration(s) {ahead} that this build does not know; deploy a newer image"
        )
    for version, (name, digest) in applied.items():
        m = known[version]
        if digest != m.checksum:
            raise MigrationError(
                f"migration {m.label} was modified after it was applied "
                f"(ledger {digest[:12]}, file {m.checksum[:12]}); add a new migration instead"
            )
    todo = [m for m in migrations if m.version not in applied]
    if todo and applied and todo[0].version < max(applied):
        raise MigrationError(f"migration {todo[0].label} is older than the applied head {max(applied):04d}")
    return todo


def _acquire_lock(conn: psycopg.Connection, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while not conn.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,)).fetchone()[0]:
        if time.monotonic() >= deadline:
            raise MigrationError(f"could not acquire the migration lock within {timeout_s:.0f}s")
        time.sleep(0.5)


def migrate(conn: psycopg.Connection, migrations: list[Migration] | None = None, *,
            lock_timeout_s: float = 60) -> list[Migration]:
    """Apply pending migrations; returns the ones applied by this call."""
    if not conn.autocommit:
        raise MigrationError("migrate() needs an autocommit connection")
    migrations = discover() if migrations is None else migrations

    _acquire_lock(conn, lock_timeout_s)
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
                version    integer PRIMARY KEY,
                name       text NOT NULL,
                checksum   text NOT NULL CHECK (checksum ~ '^[0-9a-f]{{64}}$'),
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        todo = pending(conn, migrations)
        for m in todo:
            started = time.monotonic()
            with conn.transaction():
                conn.execute(m.sql)  # no parameters: sent as a simple multi-statement query
                conn.execute(
                    f"INSERT INTO {LEDGER_TABLE} (version, name, checksum) VALUES (%s, %s, %s)",
                    (m.version, m.name, m.checksum),
                )
            log.info("migration applied", extra=fields(
                migration=m.label, ms=round((time.monotonic() - started) * 1000)))
        head = migrations[-1].label if migrations else "none"
        log.info("schema up to date", extra=fields(head=head, applied_now=len(todo)))
        return todo
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def assert_current(conn: psycopg.Connection, migrations: list[Migration] | None = None) -> None:
    """Raise unless the database schema matches this build exactly."""
    migrations = discover() if migrations is None else migrations
    todo = pending(conn, migrations)
    if todo:
        raise SchemaBehind(f"schema is behind: {len(todo)} pending migration(s), next {todo[0].label}")


def wait_until_current(conn: psycopg.Connection, *, timeout_s: float) -> None:
    """Block until the migrate job has brought the schema to this build's head.

    Compose already orders ``worker`` after ``migrate``; this is the same
    guarantee enforced by the worker itself, for hosts or orchestrators that
    do not honour ``depends_on`` conditions.
    """
    migrations = discover()
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            assert_current(conn, migrations)
            return
        except SchemaBehind as exc:
            if time.monotonic() >= deadline:
                raise
            log.info("waiting for migrations", extra=fields(reason=str(exc)))
            time.sleep(1.0)
