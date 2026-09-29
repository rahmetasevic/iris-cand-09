"""Command line entry point: ``python -m iris_pilot <command>``.

Exit codes: 0 ok, 1 run/check failed, 2 configuration error,
3 a dependency (database, schema) is not available.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

import psycopg

from . import __version__
from .config import ConfigError, load_pipeline_settings, load_runtime_settings
from .contract import SourceContractError
from .db import DatabaseUnavailable, connect, wait_for_database
from .log import configure, fields
from .migrate import MigrationError, assert_current, migrate, wait_until_current
from .pipeline import RunFailed, run_once
from .smoke import run_smoke
from .sources import SourceError

log = logging.getLogger("iris_pilot")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_UNAVAILABLE = 0, 1, 2, 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iris-pilot", description="IRIS pilot worker")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="validate the configuration and print it with secrets redacted")
    m = sub.add_parser("migrate", help="apply pending database migrations")
    m.add_argument("--check", action="store_true", help="only verify the schema is at this build's head")
    sub.add_parser("run", help="ingest the configured source for the configured scope")
    sub.add_parser("smoke", help="end-to-end self-check against the running stack")
    sub.add_parser("healthcheck", help="exit 0 if the database is reachable and the schema is current")
    return parser


def _cmd_config() -> int:
    settings = load_pipeline_settings()
    print(json.dumps(settings.redacted(), indent=2, sort_keys=True))
    return EXIT_OK


def _cmd_migrate(check: bool) -> int:
    settings = load_runtime_settings()
    configure(settings.log_level)
    with wait_for_database(settings.db, timeout_s=settings.startup_timeout_s,
                           application_name="iris-pilot-migrate") as conn:
        if check:
            assert_current(conn)
            log.info("schema is current")
        else:
            migrate(conn, lock_timeout_s=settings.startup_timeout_s)
    return EXIT_OK


def _cmd_healthcheck() -> int:
    settings = load_runtime_settings()
    try:
        with connect(settings.db, application_name="iris-pilot-healthcheck") as conn:
            assert_current(conn)
    except (psycopg.Error, MigrationError) as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def _cmd_smoke() -> int:
    settings = load_pipeline_settings()
    configure(settings.runtime.log_level)
    return EXIT_OK if run_smoke(settings) else EXIT_FAILED


def _cmd_run() -> int:
    settings = load_pipeline_settings()
    configure(settings.runtime.log_level)
    log.info("worker starting", extra=fields(version=__version__, scope=settings.scope,
                                             interval_s=settings.run_interval_s))

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    conn: psycopg.Connection | None = None
    try:
        while True:
            if conn is None or conn.closed:
                conn = wait_for_database(settings.db, timeout_s=settings.runtime.startup_timeout_s,
                                         application_name="iris-pilot-worker")
                wait_until_current(conn, timeout_s=settings.runtime.startup_timeout_s)
            try:
                run_once(conn, settings)
                code = EXIT_OK
            except (RunFailed, SourceError, SourceContractError):
                code = EXIT_FAILED  # already logged with context by the pipeline
            except psycopg.OperationalError as exc:
                if settings.run_interval_s == 0:
                    raise
                log.warning("database connection lost; reconnecting next cycle", extra=fields(error=str(exc)))
                conn.close()
                code = EXIT_FAILED
            if settings.run_interval_s == 0:
                return code
            if stop.wait(settings.run_interval_s):
                log.info("worker stopping")
                return EXIT_OK
    finally:
        if conn is not None:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "config":
            return _cmd_config()
        if args.command == "migrate":
            return _cmd_migrate(args.check)
        if args.command == "healthcheck":
            return _cmd_healthcheck()
        if args.command == "smoke":
            return _cmd_smoke()
        return _cmd_run()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return EXIT_CONFIG
    except (DatabaseUnavailable, MigrationError) as exc:
        log.error("dependency not available", extra=fields(error=str(exc)))
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except psycopg.Error as exc:
        log.error("database error", extra=fields(error=str(exc).strip()))
        return EXIT_FAILED
