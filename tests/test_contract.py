from __future__ import annotations

import json
from datetime import date

import pytest

from conftest import FIXTURES
from iris_pilot.contract import SourceContractError, parse_crs, parse_feature_collection

SQUARE = [[[10.0, 50.0], [10.001, 50.0], [10.001, 50.001], [10.0, 50.001], [10.0, 50.0]]]


def collection(*features, **members) -> bytes:
    doc = {"type": "FeatureCollection", "source_date": "2025-01-31", "features": list(features), **members}
    return json.dumps(doc).encode()


def feature(site_id="A", geometry=None, **props) -> dict:
    geom = {"type": "Polygon", "coordinates": SQUARE} if geometry is None else geometry
    return {"type": "Feature", "properties": {"site_id": site_id, **props}, "geometry": geom}


def parse(content: bytes, country="XX", region="YY"):
    return parse_feature_collection(content, country_code=country, region_code=region)


def reasons(parsed) -> list[tuple[int, str]]:
    return [(r.index, r.reason) for r in parsed.rejections]


def test_de_nw_fixture() -> None:
    parsed = parse((FIXTURES / "DE-NW" / "sites.geojson").read_bytes(), "DE", "NW")
    assert parsed.srid == 4326
    assert parsed.source_date == date(2025, 6, 30)
    assert parsed.feature_count == 6
    # The self-intersecting S-0004 is structurally fine; PostGIS rejects it later.
    assert [c.site_id for c in parsed.candidates] == ["S-0001", "S-0002", "S-0003", "S-0004", "S-0006"]
    assert reasons(parsed) == [(4, "missing_geometry")]
    by_id = {c.site_id: c for c in parsed.candidates}
    assert by_id["S-0003"].source_date == date(2025, 5, 15)
    # Missing optional attributes stay unknown; they are not defaulted.
    assert by_id["S-0002"].positional_accuracy_m is None
    assert by_id["S-0006"].name is None


def test_at_9_fixture() -> None:
    parsed = parse((FIXTURES / "AT-9" / "sites.geojson").read_bytes(), "AT", "9")
    assert parsed.srid == 4326
    assert [c.site_id for c in parsed.candidates] == ["S-0001", "S-0002", "S-0003"]
    assert reasons(parsed) == [(3, "missing_site_id"), (4, "scope_mismatch")]


def test_source_for_another_scope_is_refused_as_a_whole() -> None:
    content = (FIXTURES / "DE-NW" / "sites.geojson").read_bytes()
    with pytest.raises(SourceContractError, match="country_code"):
        parse(content, "AT", "9")


def test_source_date_is_required_not_inferred() -> None:
    doc = json.loads(collection(feature()))
    del doc["source_date"]
    with pytest.raises(SourceContractError, match="source_date"):
        parse(json.dumps(doc).encode())


@pytest.mark.parametrize("value", ["2025-02-30", "31.01.2025", "2025-1-5", 20250131])
def test_bad_source_date(value) -> None:
    with pytest.raises(SourceContractError):
        parse(collection(feature(), source_date=value))


@pytest.mark.parametrize("crs,srid", [
    (None, 4326),
    ({"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}, 4326),
    ({"type": "name", "properties": {"name": "EPSG:3857"}}, 3857),
    ({"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::25832"}}, 25832),
    ({"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG:6.6:31256"}}, 31256),
])
def test_crs_is_explicit(crs, srid) -> None:
    assert parse_crs(crs) == srid


@pytest.mark.parametrize("crs", [
    {"type": "name", "properties": {"name": "WGS84-ish"}},
    {"type": "link", "properties": {"href": "http://example.org/crs"}},
    "EPSG:4326",
])
def test_unknown_crs_is_refused(crs) -> None:
    with pytest.raises(SourceContractError):
        parse_crs(crs)


def test_duplicate_site_ids_reject_every_copy() -> None:
    parsed = parse(collection(feature("A"), feature("B"), feature("A")))
    assert [c.site_id for c in parsed.candidates] == ["B"]
    assert reasons(parsed) == [(0, "duplicate_site_id"), (2, "duplicate_site_id")]


@pytest.mark.parametrize("geometry,reason", [
    ({"type": "Point", "coordinates": [10.0, 50.0]}, "unsupported_geometry_type"),
    ({"type": "Polygon", "coordinates": [[[10, 50], [11, 50], [11, 51], [10, 51]]]}, "malformed_geometry"),
    ({"type": "Polygon", "coordinates": [[[10, 50], [11, 50], [11, 51]]]}, "malformed_geometry"),
    ({"type": "Polygon", "coordinates": [[[10, 50], [11, "50"], [11, 51], [10, 50]]]}, "malformed_geometry"),
    ({"type": "Polygon", "coordinates": []}, "malformed_geometry"),
    ({"type": "MultiPolygon", "coordinates": []}, "malformed_geometry"),
    ("POLYGON((0 0, 1 0, 1 1, 0 0))", "malformed_geometry"),
])
def test_geometry_structure(geometry, reason) -> None:
    parsed = parse(collection(feature(geometry=geometry)))
    assert reasons(parsed) == [(0, reason)]


@pytest.mark.parametrize("props,reason", [
    ({"positional_accuracy_m": -1}, "invalid_attribute"),
    ({"positional_accuracy_m": "2m"}, "invalid_attribute"),
    ({"positional_accuracy_m": True}, "invalid_attribute"),
    ({"name": 42}, "invalid_attribute"),
    ({"source_date": "yesterday"}, "invalid_source_date"),
    ({"region_code": "ZZ"}, "scope_mismatch"),
])
def test_attribute_contract(props, reason) -> None:
    assert reasons(parse(collection(feature(**props)))) == [(0, reason)]


@pytest.mark.parametrize("site_id", [None, "", "   ", True])
def test_site_id_required(site_id) -> None:
    assert reasons(parse(collection(feature(site_id=site_id)))) == [(0, "missing_site_id")]


def test_numeric_site_id_is_kept_as_text() -> None:
    assert parse(collection(feature(site_id=17))).candidates[0].site_id == "17"


def test_third_coordinate_is_accepted() -> None:
    ring = [[10.0, 50.0, 5], [10.001, 50.0, 5], [10.001, 50.001, 5], [10.0, 50.0, 5]]
    assert parse(collection(feature(geometry={"type": "Polygon", "coordinates": [ring]}))).candidates


@pytest.mark.parametrize("content", [b"not json", b'{"type": "Feature"}', b'{"type": "FeatureCollection"}',
                                     "﻿{}".encode("utf-16"),
                                     b'{"type":"FeatureCollection","source_date":"2025-01-01","features":[NaN]}'])
def test_unusable_documents(content: bytes) -> None:
    with pytest.raises(SourceContractError):
        parse(content)
