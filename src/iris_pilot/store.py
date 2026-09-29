"""SQL access for runs, candidate sites and rejections.

All statements filter on ``country_code`` (and ``region_code`` where the
table has it), so a deployment can only ever read or change its own scope.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

import psycopg

from .contract import WGS84_SRID, Candidate, ParsedSource, Rejection, SourceContractError


@dataclass(frozen=True)
class SiteRow:
    site_id: str
    name: str | None
    source_date: date
    positional_accuracy_m: float | None
    area_m2: float
    geometry_json: str


def try_lock_scope(conn: psycopg.Connection, scope: str) -> bool:
    row = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (f"iris-scope:{scope}",)).fetchone()
    return bool(row[0])


def unlock_scope(conn: psycopg.Connection, scope: str) -> None:
    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (f"iris-scope:{scope}",))


def start_run(conn: psycopg.Connection, *, country_code: str, region_code: str,
              source_uri: str) -> tuple[int, datetime]:
    row = conn.execute(
        """
        INSERT INTO iris.ingest_run (country_code, region_code, source_uri)
        VALUES (%s, %s, %s)
        RETURNING run_id, started_at
        """,
        (country_code, region_code, source_uri),
    ).fetchone()
    return int(row[0]), row[1]


def fail_run(conn: psycopg.Connection, *, country_code: str, run_id: int, error: str) -> None:
    conn.execute(
        """
        UPDATE iris.ingest_run
           SET status = 'failed', finished_at = now(), error = left(%s, 2000)
         WHERE country_code = %s AND run_id = %s AND status = 'running'
        """,
        (error, country_code, run_id),
    )


def finish_run(conn: psycopg.Connection, *, country_code: str, run_id: int, finished_at: datetime) -> None:
    updated = conn.execute(
        """
        UPDATE iris.ingest_run
           SET status = 'succeeded', finished_at = %s
         WHERE country_code = %s AND run_id = %s AND status = 'running'
        """,
        (finished_at, country_code, run_id),
    ).rowcount
    if updated != 1:
        raise RuntimeError(f"run {country_code}/{run_id} was not in state 'running'")


def validate_geometries(conn: psycopg.Connection, candidates: Sequence[Candidate], srid: int) -> list[Rejection]:
    """Check validity with GEOS via PostGIS; invalid shapes are rejected, never repaired."""
    if conn.execute("SELECT 1 FROM public.spatial_ref_sys WHERE srid = %s", (srid,)).fetchone() is None:
        raise SourceContractError(f"source CRS EPSG:{srid} is unknown to PostGIS")
    if not candidates:
        return []
    rows = conn.execute(
        """
        WITH input AS (
            SELECT t.idx, ST_SetSRID(ST_GeomFromGeoJSON(t.geom_json), %(srid)s) AS g
              FROM unnest(%(idx)s::integer[], %(geoms)s::text[]) AS t(idx, geom_json)
        ), checked AS (
            SELECT idx, g, ST_IsEmpty(g) AS is_empty, ST_IsValid(g) AS is_valid FROM input
        ), projected AS (
            SELECT idx, is_empty, is_valid,
                   CASE WHEN NOT is_valid THEN ST_IsValidReason(g) END AS reason,
                   CASE WHEN is_valid AND NOT is_empty THEN ST_Transform(g, %(wgs84)s) END AS w
              FROM checked
        )
        SELECT idx, is_empty, is_valid, reason, ST_XMin(w), ST_YMin(w), ST_XMax(w), ST_YMax(w)
          FROM projected
         ORDER BY idx
        """,
        {
            "srid": srid,
            "wgs84": WGS84_SRID,
            "idx": [c.index for c in candidates],
            "geoms": [c.geometry_json for c in candidates],
        },
    ).fetchall()

    by_index = {c.index: c for c in candidates}
    rejections: list[Rejection] = []
    for idx, is_empty, is_valid, reason, xmin, ymin, xmax, ymax in rows:
        site_id = by_index[idx].site_id
        if is_empty:
            rejections.append(Rejection(idx, site_id, "empty_geometry", "geometry is empty"))
        elif not is_valid:
            rejections.append(Rejection(idx, site_id, "invalid_geometry", reason or "invalid geometry"))
        elif xmin < -180 or xmax > 180 or ymin < -90 or ymax > 90:
            rejections.append(Rejection(idx, site_id, "coordinates_out_of_range",
                                        "geometry falls outside WGS84 lon/lat bounds after transformation"))
    return rejections


def promote(conn: psycopg.Connection, *, country_code: str, region_code: str, run_id: int,
            parsed: ParsedSource, accepted: Sequence[Candidate], rejections: Sequence[Rejection],
            source_sha256: str) -> None:
    """Replace the scope's current sites with ``accepted`` in one transaction."""
    site_ids = [c.site_id for c in accepted]
    with conn.transaction():
        conn.execute(
            """
            DELETE FROM iris.site_candidate
             WHERE country_code = %s AND region_code = %s AND site_id <> ALL(%s::text[])
            """,
            (country_code, region_code, site_ids),
        )
        conn.execute(
            """
            INSERT INTO iris.site_candidate AS s
                   (country_code, region_code, site_id, name, source_date, positional_accuracy_m,
                    area_m2, geom, ingest_run_id)
            SELECT %(country)s, %(region)s, t.site_id, t.name, t.source_date, t.accuracy,
                   ST_Area(g.geom::geography), g.geom, %(run_id)s
              FROM unnest(%(site_ids)s::text[], %(names)s::text[], %(dates)s::date[],
                          %(accuracy)s::double precision[], %(geoms)s::text[])
                   AS t(site_id, name, source_date, accuracy, geom_json)
             CROSS JOIN LATERAL (
                   SELECT ST_Multi(ST_Force2D(ST_Transform(
                              ST_SetSRID(ST_GeomFromGeoJSON(t.geom_json), %(srid)s), %(wgs84)s
                          )))::geometry(MultiPolygon, 4326) AS geom
             ) AS g
            ON CONFLICT (country_code, region_code, site_id) DO UPDATE
               SET name = EXCLUDED.name,
                   source_date = EXCLUDED.source_date,
                   positional_accuracy_m = EXCLUDED.positional_accuracy_m,
                   area_m2 = EXCLUDED.area_m2,
                   geom = EXCLUDED.geom,
                   ingest_run_id = EXCLUDED.ingest_run_id,
                   updated_at = now()
             WHERE (s.name, s.source_date, s.positional_accuracy_m, s.geom)
                   IS DISTINCT FROM
                   (EXCLUDED.name, EXCLUDED.source_date, EXCLUDED.positional_accuracy_m, EXCLUDED.geom)
            """,
            {
                "country": country_code,
                "region": region_code,
                "run_id": run_id,
                "srid": parsed.srid,
                "wgs84": WGS84_SRID,
                "site_ids": site_ids,
                "names": [c.name for c in accepted],
                "dates": [c.source_date for c in accepted],
                "accuracy": [c.positional_accuracy_m for c in accepted],
                "geoms": [c.geometry_json for c in accepted],
            },
        )
        if rejections:
            conn.execute(
                """
                INSERT INTO iris.ingest_rejection (country_code, ingest_run_id, feature_index, site_id, reason, detail)
                SELECT %(country)s, %(run_id)s, t.idx, t.site_id, t.reason, t.detail
                  FROM unnest(%(idx)s::integer[], %(site_ids)s::text[], %(reasons)s::text[], %(details)s::text[])
                       AS t(idx, site_id, reason, detail)
                """,
                {
                    "country": country_code,
                    "run_id": run_id,
                    "idx": [r.index for r in rejections],
                    "site_ids": [r.site_id for r in rejections],
                    "reasons": [r.reason for r in rejections],
                    "details": [r.detail for r in rejections],
                },
            )
        conn.execute(
            """
            UPDATE iris.ingest_run
               SET source_sha256 = %s, source_date = %s, source_srid = %s,
                   feature_count = %s, accepted_count = %s, rejected_count = %s
             WHERE country_code = %s AND run_id = %s
            """,
            (source_sha256, parsed.source_date, parsed.srid, parsed.feature_count, len(accepted),
             len(rejections), country_code, run_id),
        )


def scope_sites(conn: psycopg.Connection, *, country_code: str, region_code: str) -> list[SiteRow]:
    rows = conn.execute(
        """
        SELECT site_id, name, source_date, positional_accuracy_m, area_m2, ST_AsGeoJSON(geom, 7)
          FROM iris.site_candidate
         WHERE country_code = %s AND region_code = %s
         ORDER BY site_id COLLATE "C"
        """,
        (country_code, region_code),
    ).fetchall()
    return [SiteRow(*row) for row in rows]
