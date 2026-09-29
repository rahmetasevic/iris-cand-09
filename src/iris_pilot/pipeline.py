"""One worker run: fetch -> validate -> promote -> export, for one scope.

A run either succeeds completely (database and output files consistent,
run row ``succeeded``) or is recorded as ``failed`` with the reason. The
scope's previously promoted data is only replaced after the new source has
passed the contract and the reject-ratio gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from . import __version__, export, store
from .config import PipelineSettings, redact_uri
from .contract import Rejection, parse_feature_collection
from .log import fields
from .sources import fetch

log = logging.getLogger(__name__)


class RunFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    run_id: int
    scope: str
    feature_count: int
    accepted: int
    rejections: tuple[Rejection, ...]
    sites_path: Path
    summary_path: Path
    sites_sha256: str


def run_once(conn: psycopg.Connection, settings: PipelineSettings) -> RunResult:
    if not store.try_lock_scope(conn, settings.scope):
        raise RunFailed(f"another run for {settings.scope} is in progress")
    try:
        abandoned = store.abandon_stale_runs(conn, country_code=settings.country_code,
                                             region_code=settings.region_code)
        if abandoned:
            log.warning("closed runs abandoned by a previous worker",
                        extra=fields(scope=settings.scope, runs=abandoned))
        return _run_locked(conn, settings)
    finally:
        store.unlock_scope(conn, settings.scope)


def _run_locked(conn: psycopg.Connection, s: PipelineSettings) -> RunResult:
    cc, rc = s.country_code, s.region_code
    run_id, started_at = store.start_run(conn, country_code=cc, region_code=rc,
                                         source_uri=redact_uri(s.source_endpoint))
    log.info("run started", extra=fields(run_id=run_id, scope=s.scope))
    try:
        payload = fetch(s.source_endpoint, timeout_s=s.source_timeout_s, max_bytes=s.source_max_bytes)
        parsed = parse_feature_collection(payload.content, country_code=cc, region_code=rc)
        geometry_rejections = store.validate_geometries(conn, parsed.candidates, parsed.srid)

        rejected_idx = {r.index for r in geometry_rejections}
        accepted = [c for c in parsed.candidates if c.index not in rejected_idx]
        rejections = tuple(sorted((*parsed.rejections, *geometry_rejections), key=lambda r: r.index))

        if not accepted:
            raise RunFailed(f"no feature passed validation ({len(rejections)} rejected); existing data kept")
        ratio = len(rejections) / parsed.feature_count
        if ratio > s.max_reject_ratio:
            raise RunFailed(
                f"reject ratio {ratio:.2f} exceeds IRIS_MAX_REJECT_RATIO={s.max_reject_ratio}; existing data kept"
            )

        store.promote(conn, country_code=cc, region_code=rc, run_id=run_id, parsed=parsed,
                      accepted=accepted, rejections=rejections, source_sha256=payload.sha256)

        rows = store.scope_sites(conn, country_code=cc, region_code=rc)
        sites_doc = export.build_sites_document(rows, country_code=cc, region_code=rc,
                                                eco_points_per_m2=s.eco_points_per_m2)
        out_dir = s.scope_output_dir
        sites_path = out_dir / export.SITES_FILE
        sites_sha = export.write_atomic(sites_path, export.render(sites_doc))

        finished_at = datetime.now(UTC)
        summary: dict[str, Any] = {
            "run_id": run_id,
            "status": "succeeded",
            "scope": {"country_code": cc, "region_code": rc},
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "worker_version": __version__,
            "source": {
                "uri": payload.uri,
                "sha256": payload.sha256,
                "source_date": parsed.source_date.isoformat(),
                "declared_srid": parsed.srid,
            },
            "counts": {"features": parsed.feature_count, "accepted": len(accepted), "rejected": len(rejections)},
            "completeness": {
                "positional_accuracy_m": {
                    "known": sum(1 for c in accepted if c.positional_accuracy_m is not None),
                    "unknown": sum(1 for c in accepted if c.positional_accuracy_m is None),
                },
                "name": {
                    "known": sum(1 for c in accepted if c.name is not None),
                    "unknown": sum(1 for c in accepted if c.name is None),
                },
            },
            "rejections": [r.as_dict() for r in rejections],
            "outputs": {export.SITES_FILE: {"sha256": sites_sha, "features": len(rows)}},
            "settings": s.redacted(),
            "disclaimer": export.disclaimer(s.eco_points_per_m2),
        }
        summary_path = out_dir / export.SUMMARY_FILE
        export.write_atomic(summary_path, export.render(summary))
        # Marked succeeded only once the database and both files agree.
        store.finish_run(conn, country_code=cc, run_id=run_id, finished_at=finished_at)
    except Exception as exc:
        log.error("run failed", extra=fields(run_id=run_id, scope=s.scope, error=str(exc)))
        try:
            store.fail_run(conn, country_code=cc, run_id=run_id, error=f"{type(exc).__name__}: {exc}")
        except psycopg.Error:
            log.exception("could not record the failed run", extra=fields(run_id=run_id))
        raise

    log.info("run succeeded", extra=fields(
        run_id=run_id, scope=s.scope, features=parsed.feature_count, accepted=len(accepted),
        rejected=len(rejections), sites_sha256=sites_sha[:12], output=str(sites_path)))
    return RunResult(run_id, s.scope, parsed.feature_count, len(accepted), rejections,
                     sites_path, summary_path, sites_sha)
