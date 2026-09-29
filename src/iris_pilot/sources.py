"""Fetch source documents from ``http(s)://`` or ``file://`` endpoints.

The scheme is configuration: a deployment can point at a remote service,
a local mock container or a mounted file without any code change.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, url2pathname, urlopen

from . import __version__
from .config import redact_uri
from .log import fields

log = logging.getLogger(__name__)


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourcePayload:
    uri: str  # redacted, safe to log and persist
    content: bytes
    sha256: str


def fetch(uri: str, *, timeout_s: float, max_bytes: int, attempts: int = 3) -> SourcePayload:
    scheme = urlsplit(uri).scheme
    if scheme == "file":
        content = _read_file(uri, max_bytes)
    elif scheme in {"http", "https"}:
        content = _read_http(uri, timeout_s=timeout_s, max_bytes=max_bytes, attempts=attempts)
    else:
        raise SourceError(f"unsupported source scheme {scheme!r}")
    digest = hashlib.sha256(content).hexdigest()
    log.info("source fetched", extra=fields(uri=redact_uri(uri), bytes=len(content), sha256=digest[:12]))
    return SourcePayload(uri=redact_uri(uri), content=content, sha256=digest)


def _read_file(uri: str, max_bytes: int) -> bytes:
    path = Path(url2pathname(urlsplit(uri).path))
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise SourceError(f"source file is {size} bytes, above the {max_bytes} byte limit")
        return path.read_bytes()
    except OSError as exc:
        raise SourceError(f"cannot read source file {path}: {exc.strerror or exc}") from exc


def _read_http(uri: str, *, timeout_s: float, max_bytes: int, attempts: int) -> bytes:
    request = Request(uri, headers={
        "Accept": "application/geo+json, application/json;q=0.9",
        "User-Agent": f"iris-pilot/{__version__}",
    })
    safe_uri = redact_uri(uri)
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout_s) as response:
                content = response.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise SourceError(f"source response exceeds the {max_bytes} byte limit")
            return content
        except HTTPError as exc:
            # 4xx means the request itself is wrong; retrying will not help.
            if exc.code < 500 or attempt == attempts:
                raise SourceError(f"source {safe_uri} returned HTTP {exc.code}") from exc
            reason = f"HTTP {exc.code}"
        except (URLError, TimeoutError, ConnectionError) as exc:
            if attempt == attempts:
                raise SourceError(f"source {safe_uri} unreachable: {getattr(exc, 'reason', exc)}") from exc
            reason = str(getattr(exc, "reason", exc))
        backoff = 2 ** (attempt - 1)
        log.warning("source fetch failed, retrying",
                    extra=fields(uri=safe_uri, attempt=attempt, retry_in_s=backoff, reason=reason))
        time.sleep(backoff)
    raise AssertionError("unreachable")
