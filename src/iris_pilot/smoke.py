"""``iris-pilot smoke``: end-to-end check of a deployed stack.

Exercises the real path (config -> database -> schema -> source -> run ->
database invariants -> output files -> idempotent rerun) and prints one
PASS/FAIL line per step. It asserts invariants, not fixture specifics, so
it works unchanged for any configured country/region.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import psycopg

from .config import PipelineSettings, redact_uri
from .db import wait_for_database
from .migrate import assert_current
from .pipeline import RunResult, run_once


class SmokeFailure(AssertionError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def run_smoke(settings: PipelineSettings, out: Callable[[str], None] = print) -> bool:
    state: dict[str, Any] = {}
    cc, rc = settings.country_code, settings.region_code

    def configuration() -> str:
        return (f"scope={settings.scope} source={redact_uri(settings.source_endpoint)} "
                f"output={settings.scope_output_dir}")

    def database() -> str:
        conn = wait_for_database(settings.db, timeout_s=settings.runtime.startup_timeout_s,
                                 application_name="iris-pilot-smoke")
        state["conn"] = conn
        server, postgis = conn.execute(
            "SELECT current_setting('server_version_num')::int, postgis_lib_version()").fetchone()
        _require(server >= 160000, f"PostgreSQL {server} is older than 16")
        major_minor = tuple(int(p) for p in postgis.split(".")[:2])
        _require(major_minor >= (3, 4), f"PostGIS {postgis} is older than 3.4")
        return f"server_version_num={server} postgis={postgis}"

    def schema() -> str:
        assert_current(state["conn"])
        return "no pending migrations"

    def first_run() -> str:
        result: RunResult = run_once(state["conn"], settings)
        _require(result.accepted > 0, "run accepted no features")
        state["run"] = result
        return f"run_id={result.run_id} accepted={result.accepted} rejected={len(result.rejections)}"

    def invariants() -> str:
        conn: psycopg.Connection = state["conn"]
        result: RunResult = state["run"]
        total, bad = conn.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE ST_SRID(geom) <> 4326 OR NOT ST_IsValid(geom) OR area_m2 <= 0)
              FROM iris.site_candidate
             WHERE country_code = %s AND region_code = %s
            """, (cc, rc)).fetchone()
        _require(total == result.accepted, f"{total} rows in scope, run accepted {result.accepted}")
        _require(bad == 0, f"{bad} rows violate geometry/area invariants")
        status, rejected = conn.execute(
            """
            SELECT r.status, (SELECT count(*) FROM iris.ingest_rejection j
                               WHERE j.country_code = r.country_code AND j.ingest_run_id = r.run_id)
              FROM iris.ingest_run r
             WHERE r.country_code = %s AND r.run_id = %s
            """, (cc, result.run_id)).fetchone()
        _require(status == "succeeded", f"run row status is {status!r}")
        _require(rejected == len(result.rejections), "persisted rejections do not match the run")
        return f"{total} site(s) valid in EPSG:4326, {rejected} rejection(s) recorded"

    def outputs() -> str:
        result: RunResult = state["run"]
        sites = json.loads(result.sites_path.read_text(encoding="utf-8"))
        summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
        features = sites.get("features", [])
        _require(len(features) == result.accepted, f"{len(features)} features in output, expected {result.accepted}")
        _require(all(f["properties"]["country_code"] == cc and f["properties"]["region_code"] == rc
                     for f in features), "output contains features from another scope")
        _require(summary.get("run_id") == result.run_id, "run summary belongs to a different run")
        _require(bool(summary.get("disclaimer")), "run summary is missing the disclaimer")
        return f"{result.sites_path.name}, {result.summary_path.name}"

    def rerun() -> str:
        first: RunResult = state["run"]
        second = run_once(state["conn"], settings)
        _require(second.sites_sha256 == first.sites_sha256, "second run produced a different sites file")
        _require(second.accepted == first.accepted, "second run accepted a different number of features")
        return f"sha256={second.sites_sha256[:12]} unchanged"

    steps: list[tuple[str, Callable[[], str]]] = [
        ("configuration loads", configuration),
        ("database reachable, versions supported", database),
        ("schema at head", schema),
        ("worker run succeeds", first_run),
        ("database invariants", invariants),
        ("output files", outputs),
        ("rerun is idempotent", rerun),
    ]
    ok = True
    try:
        for name, fn in steps:
            try:
                detail = fn()
            except Exception as exc:  # report every kind of failure the same way
                out(f"FAIL  {name}: {type(exc).__name__}: {exc}")
                ok = False
                break
            out(f"PASS  {name}: {detail}")
    finally:
        if "conn" in state:
            state["conn"].close()
    out("SMOKE OK" if ok else "SMOKE FAILED")
    return ok
