"""Behaviour against a real PostgreSQL/PostGIS server.

Each test gets its own freshly created database (see conftest), so the
suite is order-independent and never touches pilot data.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import psycopg
import pytest
from psycopg import errors

from conftest import FIXTURES, make_settings
from iris_pilot.db import connect
from iris_pilot.migrate import (
    LEDGER_TABLE,
    MigrationError,
    SchemaBehind,
    assert_current,
    discover,
    migrate,
    wait_until_current,
)
from iris_pilot.pipeline import RunFailed, run_once
from iris_pilot.smoke import run_smoke

pytestmark = pytest.mark.integration

DE_NW = ("DE", "NW")
AT_9 = ("AT", "9")


def write_source(tmp_path: Path, doc: dict) -> str:
    path = tmp_path / "source.geojson"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path.as_uri()


def fixture_doc(scope: str) -> dict:
    return json.loads((FIXTURES / scope / "sites.geojson").read_text(encoding="utf-8"))


def scope_rows(conn: psycopg.Connection, country: str, region: str) -> list[tuple]:
    return conn.execute(
        "SELECT site_id, ingest_run_id FROM iris.site_candidate "
        "WHERE country_code = %s AND region_code = %s ORDER BY site_id", (country, region)).fetchall()


# --- migrations ---------------------------------------------------------------

def test_migrations_apply_once_and_are_recorded(fresh_db) -> None:
    with connect(fresh_db, application_name="t") as conn:
        applied = migrate(conn)
        assert [m.version for m in applied] == [m.version for m in discover()]
        assert migrate(conn) == []
        ledger = conn.execute(f"SELECT version, checksum FROM {LEDGER_TABLE} ORDER BY version").fetchall()
        assert ledger == [(m.version, m.checksum) for m in discover()]
        assert_current(conn)


def test_worker_refuses_an_unmigrated_database(fresh_db) -> None:
    with connect(fresh_db, application_name="t") as conn:
        with pytest.raises(SchemaBehind):
            wait_until_current(conn, timeout_s=1)


def test_edited_migration_is_detected(conn) -> None:
    conn.execute(f"UPDATE {LEDGER_TABLE} SET checksum = repeat('0', 64) WHERE version = 1")
    with pytest.raises(MigrationError, match="modified after it was applied"):
        migrate(conn)


def test_database_newer_than_code_is_detected(conn) -> None:
    conn.execute(f"INSERT INTO {LEDGER_TABLE} (version, name, checksum) VALUES (999, 'future', repeat('a', 64))")
    with pytest.raises(MigrationError, match="does not know"):
        assert_current(conn)


def test_failed_migration_leaves_no_trace(fresh_db) -> None:
    broken = [*discover()]
    broken.append(type(broken[0])(len(broken) + 1, "broken", "CREATE TABLE iris.half (id int); SELECT 1/0;", "f" * 64))
    with connect(fresh_db, application_name="t") as conn:
        with pytest.raises(errors.DivisionByZero):
            migrate(conn, broken)
        assert conn.execute("SELECT to_regclass('iris.half')").fetchone()[0] is None
        assert conn.execute(f"SELECT max(version) FROM {LEDGER_TABLE}").fetchone()[0] == len(broken) - 1


# --- schema constraints -------------------------------------------------------

@pytest.mark.parametrize("country,message", [(None, errors.NotNullViolation), ("de", errors.CheckViolation)])
def test_country_code_is_mandatory_and_canonical(conn, country, message) -> None:
    with pytest.raises(message):
        conn.execute("INSERT INTO iris.ingest_run (country_code, region_code, source_uri) VALUES (%s, 'X', 'u')",
                     (country,))


def test_invalid_geometry_cannot_be_stored(conn) -> None:
    run_id = conn.execute("INSERT INTO iris.ingest_run (country_code, region_code, source_uri) "
                          "VALUES ('XX', 'Y', 'u') RETURNING run_id").fetchone()[0]
    with pytest.raises(errors.CheckViolation):
        conn.execute(
            """
            INSERT INTO iris.site_candidate (country_code, region_code, site_id, source_date, area_m2, geom,
                                             ingest_run_id)
            VALUES ('XX', 'Y', 'bad', '2025-01-01', 1,
                    ST_Multi(ST_GeomFromText('POLYGON((0 0, 1 1, 1 0, 0 1, 0 0))', 4326)), %s)
            """, (run_id,))


def test_joins_are_country_scoped(conn) -> None:
    run_id = conn.execute("INSERT INTO iris.ingest_run (country_code, region_code, source_uri) "
                          "VALUES ('XX', 'Y', 'u') RETURNING run_id").fetchone()[0]
    # Same run id, different country: the composite foreign key refuses it.
    with pytest.raises(errors.ForeignKeyViolation):
        conn.execute(
            """
            INSERT INTO iris.site_candidate (country_code, region_code, site_id, source_date, area_m2, geom,
                                             ingest_run_id)
            VALUES ('ZZ', 'Y', 's', '2025-01-01', 1,
                    ST_Multi(ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 0))', 4326)), %s)
            """, (run_id,))


# --- pipeline -----------------------------------------------------------------

def test_fixture_run_promotes_valid_sites_and_records_rejections(conn, migrated_db, tmp_path) -> None:
    settings = make_settings(migrated_db, tmp_path, country="DE", region="NW")
    result = run_once(conn, settings)

    assert result.accepted == 4
    assert [(r.site_id, r.reason) for r in result.rejections] == [
        ("S-0004", "invalid_geometry"), ("S-0005", "missing_geometry")]
    assert [r[0] for r in scope_rows(conn, *DE_NW)] == ["S-0001", "S-0002", "S-0003", "S-0006"]

    srids, types, invalid = conn.execute(
        "SELECT array_agg(DISTINCT ST_SRID(geom)), array_agg(DISTINCT GeometryType(geom)), "
        "count(*) FILTER (WHERE NOT ST_IsValid(geom)) FROM iris.site_candidate").fetchone()
    assert srids == [4326] and types == ["MULTIPOLYGON"] and invalid == 0

    run = conn.execute("SELECT status, feature_count, accepted_count, rejected_count, source_date::text, "
                       "source_srid FROM iris.ingest_run WHERE run_id = %s", (result.run_id,)).fetchone()
    assert run == ("succeeded", 6, 4, 2, "2025-06-30", 4326)

    sites = json.loads(result.sites_path.read_text(encoding="utf-8"))
    assert [f["properties"]["site_id"] for f in sites["features"]] == ["S-0001", "S-0002", "S-0003", "S-0006"]
    first = sites["features"][0]["properties"]
    # area_m2 is rounded to 0.1 m2 in the file, eco-points use the full value.
    assert abs(first["eco_points_indicative"] - first["area_m2"] * 8) <= 1
    # ~209 m x ~200 m around 51.22 N
    assert 40_000 < first["area_m2"] < 44_000
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["counts"] == {"features": 6, "accepted": 4, "rejected": 2}
    assert summary["completeness"]["positional_accuracy_m"] == {"known": 3, "unknown": 1}
    assert "password" not in json.dumps(summary["settings"]).replace('"password": "***"', "")


def test_rerun_is_idempotent_and_leaves_rows_untouched(conn, migrated_db, tmp_path) -> None:
    settings = make_settings(migrated_db, tmp_path, country="DE", region="NW")
    first = run_once(conn, settings)
    rows_before = scope_rows(conn, *DE_NW)
    second = run_once(conn, settings)
    assert second.run_id != first.run_id
    assert second.sites_sha256 == first.sites_sha256
    assert scope_rows(conn, *DE_NW) == rows_before  # unchanged rows keep the run that last changed them


def test_scopes_are_isolated_even_with_equal_site_ids(conn, migrated_db, tmp_path) -> None:
    de = run_once(conn, make_settings(migrated_db, tmp_path, country="DE", region="NW"))
    de_rows = scope_rows(conn, *DE_NW)
    at = run_once(conn, make_settings(migrated_db, tmp_path, country="AT", region="9"))

    assert scope_rows(conn, *DE_NW) == de_rows
    assert [r[0] for r in scope_rows(conn, *AT_9)] == ["S-0001", "S-0002", "S-0003"]
    assert de.sites_path.parent.name == "DE-NW" and at.sites_path.parent.name == "AT-9"
    at_doc = json.loads(at.sites_path.read_text(encoding="utf-8"))
    assert {f["properties"]["country_code"] for f in at_doc["features"]} == {"AT"}


def test_site_dropped_from_source_is_removed(conn, migrated_db, tmp_path) -> None:
    run_once(conn, make_settings(migrated_db, tmp_path, country="DE", region="NW"))
    doc = fixture_doc("DE-NW")
    doc["features"] = [f for f in doc["features"] if f["properties"]["site_id"] != "S-0006"]
    run_once(conn, make_settings(migrated_db, tmp_path, country="DE", region="NW",
                                 endpoint=write_source(tmp_path, doc)))
    assert [r[0] for r in scope_rows(conn, *DE_NW)] == ["S-0001", "S-0002", "S-0003"]


def test_reject_ratio_gate_keeps_previous_data(conn, migrated_db, tmp_path) -> None:
    good = run_once(conn, make_settings(migrated_db, tmp_path, country="DE", region="NW"))
    rows_before = scope_rows(conn, *DE_NW)
    sites_before = good.sites_path.read_bytes()

    doc = fixture_doc("DE-NW")
    for f in doc["features"][1:]:
        f["geometry"] = None
    settings = make_settings(migrated_db, tmp_path, country="DE", region="NW", endpoint=write_source(tmp_path, doc))
    with pytest.raises(RunFailed, match="reject ratio"):
        run_once(conn, settings)

    assert scope_rows(conn, *DE_NW) == rows_before
    assert good.sites_path.read_bytes() == sites_before
    status, error = conn.execute("SELECT status, error FROM iris.ingest_run ORDER BY run_id DESC LIMIT 1").fetchone()
    assert status == "failed" and "reject ratio" in error


def test_contract_violation_fails_the_run_without_changes(conn, migrated_db, tmp_path) -> None:
    doc = fixture_doc("DE-NW")
    del doc["source_date"]
    settings = make_settings(migrated_db, tmp_path, country="DE", region="NW", endpoint=write_source(tmp_path, doc))
    with pytest.raises(Exception, match="source_date"):
        run_once(conn, settings)
    assert scope_rows(conn, *DE_NW) == []
    assert conn.execute("SELECT status FROM iris.ingest_run").fetchall() == [("failed",)]
    assert not (tmp_path / "DE-NW").exists()


def test_unreachable_source_is_a_recorded_failure(conn, migrated_db, tmp_path) -> None:
    settings = make_settings(migrated_db, tmp_path, country="DE", region="NW",
                             endpoint=(tmp_path / "absent.geojson").as_uri())
    with pytest.raises(Exception, match="cannot read"):
        run_once(conn, settings)
    assert conn.execute("SELECT status FROM iris.ingest_run").fetchall() == [("failed",)]


def test_projected_source_is_transformed_to_wgs84(conn, migrated_db, tmp_path) -> None:
    def to_3857(lon: float, lat: float) -> list[float]:
        r = 6378137.0
        return [r * math.radians(lon), r * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))]

    ring = [to_3857(*p) for p in [(10.0, 50.0), (10.01, 50.0), (10.01, 50.01), (10.0, 50.01), (10.0, 50.0)]]
    doc = {
        "type": "FeatureCollection", "source_date": "2025-03-01",
        "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
        "features": [{"type": "Feature", "properties": {"site_id": "M-1"},
                      "geometry": {"type": "Polygon", "coordinates": [ring]}}],
    }
    settings = make_settings(migrated_db, tmp_path, country="XX", region="Y", endpoint=write_source(tmp_path, doc))
    run_once(conn, settings)
    xmin, ymin, xmax, ymax, area = conn.execute(
        "SELECT ST_XMin(geom), ST_YMin(geom), ST_XMax(geom), ST_YMax(geom), area_m2 FROM iris.site_candidate"
    ).fetchone()
    assert (round(xmin, 6), round(ymin, 6), round(xmax, 6), round(ymax, 6)) == (10.0, 50.0, 10.01, 50.01)
    assert 790_000 < area < 800_000  # ~716 m x ~1112 m at 50 N


def test_unknown_srid_is_refused(conn, migrated_db, tmp_path) -> None:
    doc = fixture_doc("DE-NW")
    doc["crs"] = {"type": "name", "properties": {"name": "EPSG:999999"}}
    settings = make_settings(migrated_db, tmp_path, country="DE", region="NW", endpoint=write_source(tmp_path, doc))
    with pytest.raises(Exception, match="unknown to PostGIS"):
        run_once(conn, settings)


def test_concurrent_run_for_same_scope_is_refused(conn, migrated_db, tmp_path) -> None:
    with connect(migrated_db, application_name="other") as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended('iris-scope:DE-NW', 0))")
        with pytest.raises(RunFailed, match="in progress"):
            run_once(conn, make_settings(migrated_db, tmp_path, country="DE", region="NW"))


def test_smoke_passes_for_each_committed_scope(migrated_db, tmp_path) -> None:
    for country, region in (DE_NW, AT_9):
        lines: list[str] = []
        assert run_smoke(make_settings(migrated_db, tmp_path, country=country, region=region), out=lines.append), lines
        assert lines[-1] == "SMOKE OK"
        assert sum(line.startswith("PASS") for line in lines) == 7
