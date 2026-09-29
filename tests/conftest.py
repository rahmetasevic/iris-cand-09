from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from iris_pilot.config import (
    RUNTIME_KEYS,
    ConfigError,
    DatabaseSettings,
    PipelineSettings,
    RuntimeSettings,
    load_runtime_settings,
)
from iris_pilot.db import connect
from iris_pilot.migrate import migrate

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures"


def fixture_uri(scope: str) -> str:
    return (FIXTURES / scope / "sites.geojson").as_uri()


def make_settings(db: DatabaseSettings, output_dir: Path, *, country: str, region: str,
                  endpoint: str | None = None, **overrides) -> PipelineSettings:
    return PipelineSettings(
        runtime=RuntimeSettings(db=db, startup_timeout_s=5),
        country_code=country,
        region_code=region,
        source_endpoint=endpoint or fixture_uri(f"{country}-{region}"),
        output_dir=output_dir,
        **overrides,
    )


@pytest.fixture(scope="session")
def admin_db() -> DatabaseSettings:
    """Server from IRIS_DB_*; integration tests skip without one unless IRIS_TEST_REQUIRE_DB=1."""
    required = os.environ.get("IRIS_TEST_REQUIRE_DB") == "1"
    env = {k: v for k, v in os.environ.items() if k in RUNTIME_KEYS}
    try:
        settings = load_runtime_settings(env)
        with connect(settings.db, application_name="iris-tests") as conn:
            conn.execute("SELECT 1")
    except (ConfigError, psycopg.Error) as exc:
        if required:
            pytest.fail(f"IRIS_TEST_REQUIRE_DB=1 but the database is not usable: {exc}")
        reason = "; ".join(exc.problems) if isinstance(exc, ConfigError) else str(exc).splitlines()[0]
        pytest.skip(f"no database configured or reachable ({reason})")
    return settings.db


@pytest.fixture
def fresh_db(admin_db: DatabaseSettings) -> Iterator[DatabaseSettings]:
    """A throw-away database, so tests never touch pilot data."""
    name = f"iris_test_{uuid.uuid4().hex[:12]}"
    with connect(admin_db, application_name="iris-tests") as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield admin_db.for_database(name)
    finally:
        with connect(admin_db, application_name="iris-tests") as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def migrated_db(fresh_db: DatabaseSettings) -> DatabaseSettings:
    with connect(fresh_db, application_name="iris-tests") as conn:
        migrate(conn)
    return fresh_db


@pytest.fixture
def conn(migrated_db: DatabaseSettings) -> Iterator[psycopg.Connection]:
    with connect(migrated_db, application_name="iris-tests") as c:
        yield c
