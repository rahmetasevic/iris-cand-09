"""Source data contract for candidate-site feature collections.

A source is a GeoJSON ``FeatureCollection`` with these extra members:

* ``source_date`` (required, ISO date): reference date of the data.
* ``country_code`` / ``region_code`` (optional): if present they must match
  the configured scope, which guards against pointing a deployment at the
  wrong region's feed.
* ``crs`` (optional, legacy GeoJSON 2008 form): absent means RFC 7946
  WGS84 lon/lat. Anything we cannot identify unambiguously is refused.

Each feature needs ``properties.site_id`` and a Polygon or MultiPolygon
geometry. Optional properties: ``name``, ``source_date`` (overrides the
collection date) and ``positional_accuracy_m`` (metres, >= 0).

Problems with the collection as a whole raise ``SourceContractError``.
Problems with a single feature produce a ``Rejection`` and the feature is
left out. Missing optional attributes stay missing: nothing is defaulted
or invented.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any

WGS84_SRID = 4326
SUPPORTED_GEOMETRY_TYPES = frozenset({"Polygon", "MultiPolygon"})

_CRS84_NAMES = frozenset({"urn:ogc:def:crs:OGC:1.3:CRS84", "urn:ogc:def:crs:OGC::CRS84", "CRS84", "OGC:CRS84"})
_EPSG_RE = re.compile(r"^(?:EPSG:|urn:ogc:def:crs:EPSG:[0-9.]*:)(\d{4,6})$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SourceContractError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    index: int
    site_id: str
    name: str | None
    source_date: date
    positional_accuracy_m: float | None
    geometry_json: str


@dataclass(frozen=True)
class Rejection:
    index: int
    site_id: str | None
    reason: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "site_id": self.site_id, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class ParsedSource:
    srid: int
    source_date: date
    feature_count: int
    candidates: tuple[Candidate, ...]
    rejections: tuple[Rejection, ...]


class _Reject(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def parse_crs(crs: Any) -> int:
    if crs is None:
        return WGS84_SRID
    name = crs.get("properties", {}).get("name") if isinstance(crs, dict) and crs.get("type") == "name" else None
    if not isinstance(name, str):
        raise SourceContractError("unsupported 'crs' member; only named CRS (EPSG / OGC CRS84) are accepted")
    if name in _CRS84_NAMES:
        return WGS84_SRID
    match = _EPSG_RE.fullmatch(name)
    if not match:
        raise SourceContractError(f"unrecognised CRS name {name!r}")
    return int(match.group(1))


def _parse_date(value: Any, what: str) -> date:
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        raise ValueError(f"{what} must be an ISO date string (YYYY-MM-DD), got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{what} {value!r} is not a valid calendar date") from None


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite number {token} is not valid JSON")


def _check_position(pos: Any) -> None:
    if not isinstance(pos, list) or not 2 <= len(pos) <= 3:
        raise _Reject("malformed_geometry", "positions must have 2 or 3 coordinates")
    for value in pos:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise _Reject("malformed_geometry", "coordinates must be finite numbers")


def _check_polygon(rings: Any) -> None:
    if not isinstance(rings, list) or not rings:
        raise _Reject("malformed_geometry", "a polygon needs at least one linear ring")
    for ring in rings:
        if not isinstance(ring, list) or len(ring) < 4:
            raise _Reject("malformed_geometry", "linear rings need at least 4 positions")
        for pos in ring:
            _check_position(pos)
        if ring[0] != ring[-1]:
            raise _Reject("malformed_geometry", "linear rings must be closed (first position == last)")


def _check_geometry(geometry: Any) -> str:
    if geometry is None:
        raise _Reject("missing_geometry", "feature has no geometry")
    if not isinstance(geometry, dict):
        raise _Reject("malformed_geometry", "geometry must be an object")
    kind = geometry.get("type")
    if kind not in SUPPORTED_GEOMETRY_TYPES:
        raise _Reject("unsupported_geometry_type",
                      f"geometry type {kind!r} is not one of {sorted(SUPPORTED_GEOMETRY_TYPES)}")
    coords = geometry.get("coordinates")
    if kind == "Polygon":
        _check_polygon(coords)
    else:
        if not isinstance(coords, list) or not coords:
            raise _Reject("malformed_geometry", "a multipolygon needs at least one polygon")
        for polygon in coords:
            _check_polygon(polygon)
    # Only type + coordinates are forwarded; any per-geometry crs is ignored
    # in favour of the collection-level declaration.
    return json.dumps({"type": kind, "coordinates": coords}, separators=(",", ":"))


def _site_id(props: dict[str, Any]) -> str | None:
    value = props.get("site_id")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_feature(index: int, feature: Any, *, country_code: str, region_code: str,
                   collection_date: date) -> Candidate:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise _Reject("malformed_feature", "entry is not a GeoJSON Feature")
    props = feature.get("properties")
    if props is None:
        props = {}
    if not isinstance(props, dict):
        raise _Reject("malformed_feature", "properties must be an object or null")

    site_id = _site_id(props)
    if site_id is None:
        raise _Reject("missing_site_id", "properties.site_id must be a non-empty string")

    for key, expected in (("country_code", country_code), ("region_code", region_code)):
        if key in props and props[key] != expected:
            raise _Reject("scope_mismatch", f"properties.{key}={props[key]!r} but this deployment is {expected!r}")

    geometry_json = _check_geometry(feature.get("geometry"))

    name = props.get("name")
    if name is not None and not isinstance(name, str):
        raise _Reject("invalid_attribute", "properties.name must be a string")

    source_date = collection_date
    if props.get("source_date") is not None:
        try:
            source_date = _parse_date(props["source_date"], "properties.source_date")
        except ValueError as exc:
            raise _Reject("invalid_source_date", str(exc)) from None

    accuracy = props.get("positional_accuracy_m")
    if accuracy is not None:
        if isinstance(accuracy, bool) or not isinstance(accuracy, (int, float)) \
                or not math.isfinite(accuracy) or accuracy < 0:
            raise _Reject("invalid_attribute", "properties.positional_accuracy_m must be a number >= 0")
        accuracy = float(accuracy)

    return Candidate(index, site_id, name, source_date, accuracy, geometry_json)


def parse_feature_collection(content: bytes, *, country_code: str, region_code: str) -> ParsedSource:
    try:
        doc = json.loads(content.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceContractError(f"source is not valid UTF-8 JSON: {exc}") from None

    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection":
        raise SourceContractError("source must be a GeoJSON FeatureCollection")
    features = doc.get("features")
    if not isinstance(features, list):
        raise SourceContractError("FeatureCollection.features must be an array")

    for key, expected in (("country_code", country_code), ("region_code", region_code)):
        if key in doc and doc[key] != expected:
            raise SourceContractError(
                f"source declares {key}={doc[key]!r} but this deployment is configured for {expected!r}"
            )

    if "source_date" not in doc:
        raise SourceContractError("source must declare 'source_date' (YYYY-MM-DD); it is not inferred")
    try:
        collection_date = _parse_date(doc["source_date"], "source_date")
    except ValueError as exc:
        raise SourceContractError(str(exc)) from None

    srid = parse_crs(doc.get("crs"))

    candidates: list[Candidate] = []
    rejections: list[Rejection] = []
    for index, feature in enumerate(features):
        try:
            candidates.append(_parse_feature(index, feature, country_code=country_code,
                                             region_code=region_code, collection_date=collection_date))
        except _Reject as exc:
            props = feature.get("properties") if isinstance(feature, dict) else None
            rejections.append(Rejection(index, _site_id(props) if isinstance(props, dict) else None,
                                        exc.reason, exc.detail))

    # An identifier that appears twice is ambiguous; keeping either copy would
    # be a silent choice, so every occurrence is rejected.
    counts = Counter(c.site_id for c in candidates)
    duplicates = {site_id for site_id, n in counts.items() if n > 1}
    if duplicates:
        for c in candidates:
            if c.site_id in duplicates:
                rejections.append(Rejection(c.index, c.site_id, "duplicate_site_id",
                                            f"site_id {c.site_id!r} occurs {counts[c.site_id]} times"))
        candidates = [c for c in candidates if c.site_id not in duplicates]

    return ParsedSource(
        srid=srid,
        source_date=collection_date,
        feature_count=len(features),
        candidates=tuple(candidates),
        rejections=tuple(sorted(rejections, key=lambda r: r.index)),
    )
