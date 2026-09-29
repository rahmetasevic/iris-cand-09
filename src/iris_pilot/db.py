"""Database connections with bounded, fail-fast startup retries."""

from __future__ import annotations

import logging
import time

import psycopg

from .config import DatabaseSettings
from .log import fields

log = logging.getLogger(__name__)

# Server rejections that waiting will not fix. libpq exposes no SQLSTATE for
# failures during connection startup, so the server message is matched.
# "the database system is starting up" is deliberately absent: it is the
# normal transient state while the container initialises.
_PERMANENT_REJECTIONS = (
    "password authentication failed",
    "does not exist",  # role or database
    "no pg_hba.conf entry",
)


class DatabaseUnavailable(RuntimeError):
    pass


def connect(db: DatabaseSettings, *, application_name: str, autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(autocommit=autocommit, **db.connect_kwargs(application_name))


def wait_for_database(db: DatabaseSettings, *, timeout_s: float, application_name: str) -> psycopg.Connection:
    """Connect, retrying transient failures with capped exponential backoff."""
    deadline = time.monotonic() + timeout_s
    delay = 0.5
    attempt = 0
    while True:
        attempt += 1
        try:
            conn = connect(db, application_name=application_name)
            conn.execute("SELECT 1")
            if attempt > 1:
                log.info("database reachable", extra=fields(attempts=attempt, host=db.host, db=db.name))
            return conn
        except psycopg.OperationalError as exc:
            if any(marker in str(exc) for marker in _PERMANENT_REJECTIONS):
                raise DatabaseUnavailable(f"database rejected the connection: {_first_line(exc)}") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DatabaseUnavailable(
                    f"database {db.host}:{db.port}/{db.name} not reachable after {attempt} attempt(s) "
                    f"in {timeout_s:.0f}s: {_first_line(exc)}"
                ) from exc
            log.warning(
                "database not ready, retrying",
                extra=fields(attempt=attempt, retry_in_s=round(min(delay, remaining), 1), reason=_first_line(exc)),
            )
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 5.0)


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__
