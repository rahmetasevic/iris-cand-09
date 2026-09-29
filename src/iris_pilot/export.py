"""Scope outputs written below ``IRIS_OUTPUT_DIR/<country>-<region>/``.

``sites.geojson`` is byte-for-byte deterministic for the same database
state (stable ordering, fixed coordinate precision, sorted keys, no
timestamps), so reruns can be compared by checksum.
``run-summary.json`` carries the audit data for one run.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from .store import SiteRow

SITES_FILE = "sites.geojson"
SUMMARY_FILE = "run-summary.json"

DISCLAIMER = (
    "Preliminary prospecting material. Figures, eco-point estimates and site suitability are indicative "
    "and based on available source data and commercial screening assumptions. The {factor:g} eco-points/m2 "
    "factor is the current commercial baseline, not certified compensation. Ownership, planning, grid "
    "capacity, environmental eligibility and transferability remain subject to project-specific "
    "verification. No permit, reservation or construction readiness is represented."
)


def disclaimer(eco_points_per_m2: float) -> str:
    return DISCLAIMER.format(factor=eco_points_per_m2)


def build_sites_document(rows: Sequence[SiteRow], *, country_code: str, region_code: str,
                         eco_points_per_m2: float) -> dict[str, Any]:
    features = []
    for row in rows:
        features.append({
            "type": "Feature",
            "id": f"{country_code}-{region_code}:{row.site_id}",
            "geometry": json.loads(row.geometry_json),
            "properties": {
                "country_code": country_code,
                "region_code": region_code,
                "site_id": row.site_id,
                "name": row.name,
                "source_date": row.source_date.isoformat(),
                "area_m2": round(row.area_m2, 1),
                "eco_points_indicative": round(row.area_m2 * eco_points_per_m2),
                "positional_accuracy_m": row.positional_accuracy_m,
            },
        })
    return {
        "type": "FeatureCollection",
        "country_code": country_code,
        "region_code": region_code,
        "metadata": {
            "crs": "EPSG:4326 (RFC 7946 lon/lat)",
            "units": {
                "area_m2": "square metres, geodesic on the WGS84 spheroid",
                "positional_accuracy_m": "metres as declared by the source; null means unknown",
                "eco_points_indicative": "area_m2 x eco_points_per_m2, rounded",
            },
            "eco_points_per_m2": eco_points_per_m2,
            "disclaimer": disclaimer(eco_points_per_m2),
        },
        "features": features,
    }


def render(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def write_atomic(path: Path, data: bytes) -> str:
    """Write via a temp file + rename so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp creates 0600; outputs are meant to be read by the host user.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return hashlib.sha256(data).hexdigest()
