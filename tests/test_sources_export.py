from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from iris_pilot import export, sources
from iris_pilot.sources import SourceError, fetch
from iris_pilot.store import SiteRow


@pytest.fixture
def http_server(monkeypatch):
    monkeypatch.setattr(sources.time, "sleep", lambda _s: None)
    calls: list[str] = []
    responses: dict[str, tuple[int, bytes]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            calls.append(self.path)
            status, body = responses.get(self.path.split("?", 1)[0], (404, b"missing"))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", responses, calls
    server.shutdown()
    server.server_close()


def test_http_fetch(http_server) -> None:
    base, responses, _ = http_server
    responses["/x.geojson"] = (200, b"{}")
    payload = fetch(f"{base}/x.geojson?token=abc", timeout_s=5, max_bytes=100)
    assert payload.content == b"{}"
    assert payload.sha256 == hashlib.sha256(b"{}").hexdigest()
    assert "abc" not in payload.uri


def test_http_client_errors_are_not_retried(http_server) -> None:
    base, _, calls = http_server
    with pytest.raises(SourceError, match="404"):
        fetch(f"{base}/nope", timeout_s=5, max_bytes=100)
    assert len(calls) == 1


def test_http_server_errors_are_retried(http_server) -> None:
    base, responses, calls = http_server
    responses["/flaky"] = (503, b"busy")
    with pytest.raises(SourceError, match="503"):
        fetch(f"{base}/flaky", timeout_s=5, max_bytes=100, attempts=3)
    assert len(calls) == 3


def test_http_size_limit(http_server) -> None:
    base, responses, _ = http_server
    responses["/big"] = (200, b"x" * 101)
    with pytest.raises(SourceError, match="limit"):
        fetch(f"{base}/big", timeout_s=5, max_bytes=100)


def test_file_fetch_and_limits(tmp_path) -> None:
    path = tmp_path / "s.geojson"
    path.write_bytes(b"{}")
    assert fetch(path.as_uri(), timeout_s=1, max_bytes=10).content == b"{}"
    with pytest.raises(SourceError, match="limit"):
        fetch(path.as_uri(), timeout_s=1, max_bytes=1)
    with pytest.raises(SourceError, match="cannot read"):
        fetch((tmp_path / "missing.geojson").as_uri(), timeout_s=1, max_bytes=10)


def rows() -> list[SiteRow]:
    geometry = json.dumps({"type": "MultiPolygon", "coordinates": [[[[1, 2], [3, 2], [3, 4], [1, 2]]]]})
    return [
        SiteRow("S-2", None, date(2025, 1, 1), None, 1000.04, geometry),
        SiteRow("S-1", "Yard", date(2025, 1, 2), 2.5, 12345.678, geometry),
    ]


def test_sites_document_is_labelled_and_deterministic() -> None:
    first = export.render(export.build_sites_document(rows(), country_code="XX", region_code="Y",
                                                      eco_points_per_m2=8))
    second = export.render(export.build_sites_document(rows(), country_code="XX", region_code="Y",
                                                       eco_points_per_m2=8))
    assert first == second
    doc = json.loads(first)
    props = doc["features"][1]["properties"]
    assert props["eco_points_indicative"] == round(12345.678 * 8)
    assert props["area_m2"] == 12345.7
    assert doc["features"][0]["properties"]["positional_accuracy_m"] is None
    assert "not certified compensation" in doc["metadata"]["disclaimer"]
    assert "8 eco-points/m2" in doc["metadata"]["disclaimer"]


def test_disclaimer_follows_the_configured_factor() -> None:
    assert "6.5 eco-points/m2" in export.disclaimer(6.5)


def test_atomic_write(tmp_path) -> None:
    target = tmp_path / "out" / "sites.geojson"
    digest = export.write_atomic(target, b"one")
    export.write_atomic(target, b"two")
    assert target.read_bytes() == b"two"
    assert digest == hashlib.sha256(b"one").hexdigest()
    assert [p.name for p in target.parent.iterdir()] == ["sites.geojson"]
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o644
