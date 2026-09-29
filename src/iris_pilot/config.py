"""Runtime configuration.

Every deployment-specific value (country, region, source endpoint, database
location and credentials, output path, tunables) comes from ``IRIS_*``
environment variables. The code base carries no knowledge of a particular
country, region, host path or database server.

Loading is strict on purpose:

* all problems are collected and reported together, so a broken ``.env``
  is fixed in one round trip instead of one variable at a time;
* unknown ``IRIS_*`` variables are an error, so a typo such as
  ``IRIS_COUNTY_CODE`` cannot silently fall back to something else;
* secrets are wrapped so they never end up in ``repr()``, logs or
  run summaries.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PREFIX = "IRIS_"

# Variables consumed by the Python runtime.
RUNTIME_KEYS = frozenset(
    {
        "IRIS_DB_HOST",
        "IRIS_DB_PORT",
        "IRIS_DB_NAME",
        "IRIS_DB_USER",
        "IRIS_DB_PASSWORD",
        "IRIS_DB_PASSWORD_FILE",
        "IRIS_DB_SSLMODE",
        "IRIS_DB_CONNECT_TIMEOUT_S",
        "IRIS_LOG_LEVEL",
        "IRIS_STARTUP_TIMEOUT_S",
    }
)
PIPELINE_KEYS = frozenset(
    {
        "IRIS_COUNTRY_CODE",
        "IRIS_REGION_CODE",
        "IRIS_SOURCE_ENDPOINT",
        "IRIS_SOURCE_TIMEOUT_S",
        "IRIS_SOURCE_MAX_BYTES",
        "IRIS_OUTPUT_DIR",
        "IRIS_ECO_POINTS_PER_M2",
        "IRIS_MAX_REJECT_RATIO",
        "IRIS_RUN_INTERVAL_S",
    }
)
# Variables only interpolated by docker compose on the host. They are
# tolerated here so that a developer can `source .env` and still run the
# CLI locally.
DEPLOYMENT_KEYS = frozenset(
    {
        "IRIS_IMAGE_TAG",
        "IRIS_POSTGIS_IMAGE",
        "IRIS_UID",
        "IRIS_GID",
        "IRIS_DB_SECRET_FILE",
        "IRIS_SOURCE_MOUNT",
        "IRIS_OUTPUT_MOUNT",
        "IRIS_WORKER_RESTART",
    }
)
KNOWN_KEYS = RUNTIME_KEYS | PIPELINE_KEYS | DEPLOYMENT_KEYS

COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")  # ISO 3166-1 alpha-2
REGION_CODE_RE = re.compile(r"^[A-Z0-9]{1,3}$")  # ISO 3166-2 subdivision suffix
SOURCE_SCHEMES = frozenset({"http", "https", "file"})
SOURCE_PLACEHOLDERS = {"country_code", "region_code"}
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
SSL_MODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})


class ConfigError(ValueError):
    """Raised with the complete list of configuration problems."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"invalid configuration ({len(self.problems)} problem(s)):\n{lines}")


class Secret:
    """A string that refuses to print itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: Secret = field(repr=False)
    sslmode: str = "prefer"
    connect_timeout_s: int = 5

    def connect_kwargs(self, application_name: str) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.name,
            "user": self.user,
            "password": self.password.reveal(),
            "sslmode": self.sslmode,
            "connect_timeout": self.connect_timeout_s,
            "application_name": application_name,
            # Pin the session time zone so timestamps never depend on the host.
            "options": "-c TimeZone=UTC",
        }

    def for_database(self, name: str) -> DatabaseSettings:
        return replace(self, name=name)

    def redacted(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "name": self.name,
            "user": self.user,
            "password": "***",
            "sslmode": self.sslmode,
            "connect_timeout_s": self.connect_timeout_s,
        }


@dataclass(frozen=True)
class RuntimeSettings:
    """What every command needs: database access and process behaviour."""

    db: DatabaseSettings
    log_level: str = "INFO"
    startup_timeout_s: int = 60

    def redacted(self) -> dict[str, Any]:
        return {
            "db": self.db.redacted(),
            "log_level": self.log_level,
            "startup_timeout_s": self.startup_timeout_s,
        }


@dataclass(frozen=True)
class PipelineSettings:
    """Runtime settings plus the scope and source of one pilot deployment."""

    runtime: RuntimeSettings
    country_code: str
    region_code: str
    source_endpoint: str
    output_dir: Path
    eco_points_per_m2: float = 8.0
    max_reject_ratio: float = 0.5
    source_timeout_s: int = 30
    source_max_bytes: int = 50_000_000
    run_interval_s: int = 0

    def __repr__(self) -> str:
        # The endpoint may embed credentials or tokens; never show it raw.
        return f"PipelineSettings({self.redacted()!r})"

    @property
    def db(self) -> DatabaseSettings:
        return self.runtime.db

    @property
    def scope(self) -> str:
        return f"{self.country_code}-{self.region_code}"

    @property
    def scope_output_dir(self) -> Path:
        return self.output_dir / self.scope

    def redacted(self) -> dict[str, Any]:
        return {
            **self.runtime.redacted(),
            "country_code": self.country_code,
            "region_code": self.region_code,
            "scope": self.scope,
            "source_endpoint": redact_uri(self.source_endpoint),
            "source_timeout_s": self.source_timeout_s,
            "source_max_bytes": self.source_max_bytes,
            "output_dir": str(self.output_dir),
            "eco_points_per_m2": self.eco_points_per_m2,
            "max_reject_ratio": self.max_reject_ratio,
            "run_interval_s": self.run_interval_s,
        }


def redact_uri(uri: str) -> str:
    """Drop credentials and query values that may carry tokens."""
    parts = urlsplit(uri)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    query = "***" if parts.query else ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


class _Reader:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env
        self.problems: list[str] = []

    def raw(self, key: str, *, strip: bool = True) -> str | None:
        assert key in KNOWN_KEYS, key
        value = self.env.get(key)
        if value is None:
            return None
        if strip:
            value = value.strip()
        return value or None

    def required(self, key: str) -> str:
        value = self.raw(key)
        if value is None:
            self.problems.append(f"{key} is required")
            return ""
        return value

    def optional(self, key: str, default: str) -> str:
        value = self.raw(key)
        return default if value is None else value

    def choice(self, key: str, default: str, allowed: frozenset[str], *, upper: bool = False) -> str:
        value = self.optional(key, default)
        if upper:
            value = value.upper()
        if value not in allowed:
            self.problems.append(f"{key}={value!r} must be one of {sorted(allowed)}")
        return value

    def integer(self, key: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
        value = self.raw(key)
        if value is None:
            return default
        try:
            number = int(value)
        except ValueError:
            self.problems.append(f"{key}={value!r} is not an integer")
            return default
        if number < minimum or (maximum is not None and number > maximum):
            upper = "" if maximum is None else f" and <= {maximum}"
            self.problems.append(f"{key}={number} must be >= {minimum}{upper}")
        return number

    def number(self, key: str, default: float, *, minimum: float, maximum: float | None = None,
               exclusive_minimum: bool = False) -> float:
        value = self.raw(key)
        if value is None:
            return default
        try:
            number = float(value)
        except ValueError:
            self.problems.append(f"{key}={value!r} is not a number")
            return default
        too_low = number <= minimum if exclusive_minimum else number < minimum
        if number != number or too_low or (maximum is not None and number > maximum):
            low = f"> {minimum}" if exclusive_minimum else f">= {minimum}"
            upper = "" if maximum is None else f" and <= {maximum}"
            self.problems.append(f"{key}={value!r} must be {low}{upper}")
        return number

    def pattern(self, key: str, regex: re.Pattern[str], hint: str) -> str:
        value = self.required(key)
        if value and not regex.fullmatch(value):
            self.problems.append(f"{key}={value!r} is invalid: {hint}")
        return value


def _read_password(r: _Reader) -> Secret:
    path = r.raw("IRIS_DB_PASSWORD_FILE")
    inline = r.raw("IRIS_DB_PASSWORD", strip=False)
    if path and inline:
        r.problems.append("set only one of IRIS_DB_PASSWORD_FILE and IRIS_DB_PASSWORD")
        return Secret("")
    if path:
        try:
            # Strip exactly what the postgres image strips from *_FILE secrets
            # (trailing LFs), so both sides always agree on the password.
            with open(path, encoding="utf-8", newline="") as fh:
                value = fh.read().rstrip("\n")
        except OSError as exc:
            r.problems.append(f"IRIS_DB_PASSWORD_FILE cannot be read ({exc.strerror or exc})")
            return Secret("")
        if not value:
            r.problems.append("IRIS_DB_PASSWORD_FILE points to an empty file")
        return Secret(value)
    if inline:
        return Secret(inline)
    r.problems.append("IRIS_DB_PASSWORD_FILE (preferred) or IRIS_DB_PASSWORD is required")
    return Secret("")


def _read_runtime(r: _Reader) -> RuntimeSettings:
    db = DatabaseSettings(
        host=r.required("IRIS_DB_HOST"),
        port=r.integer("IRIS_DB_PORT", 5432, minimum=1, maximum=65535),
        name=r.required("IRIS_DB_NAME"),
        user=r.required("IRIS_DB_USER"),
        password=_read_password(r),
        sslmode=r.choice("IRIS_DB_SSLMODE", "prefer", SSL_MODES),
        connect_timeout_s=r.integer("IRIS_DB_CONNECT_TIMEOUT_S", 5, minimum=1, maximum=300),
    )
    return RuntimeSettings(
        db=db,
        log_level=r.choice("IRIS_LOG_LEVEL", "INFO", LOG_LEVELS, upper=True),
        startup_timeout_s=r.integer("IRIS_STARTUP_TIMEOUT_S", 60, minimum=1, maximum=3600),
    )


def resolve_source_endpoint(template: str, country_code: str, region_code: str) -> str:
    """Expand ``{country_code}`` / ``{region_code}`` in an endpoint template."""
    found = set(re.findall(r"\{([^{}]*)\}", template))
    unknown = found - SOURCE_PLACEHOLDERS
    if unknown:
        raise ValueError(f"unknown placeholder(s) {sorted(unknown)}; allowed: {sorted(SOURCE_PLACEHOLDERS)}")
    resolved = template.replace("{country_code}", country_code).replace("{region_code}", region_code)
    if "{" in resolved or "}" in resolved:
        raise ValueError("unbalanced braces")
    return resolved


def _check_endpoint(r: _Reader, endpoint: str) -> None:
    parts = urlsplit(endpoint)
    if parts.scheme not in SOURCE_SCHEMES:
        r.problems.append(
            f"IRIS_SOURCE_ENDPOINT scheme {parts.scheme!r} is not supported; use one of {sorted(SOURCE_SCHEMES)}"
        )
    elif parts.scheme in {"http", "https"} and not parts.hostname:
        r.problems.append("IRIS_SOURCE_ENDPOINT must include a host name")
    elif parts.scheme == "file" and (parts.netloc not in {"", "localhost"} or not parts.path.startswith("/")):
        r.problems.append("IRIS_SOURCE_ENDPOINT file URIs must be absolute (file:///path/to/file)")


def _raise_if(r: _Reader) -> None:
    unknown = sorted(k for k in r.env if k.startswith(PREFIX) and k not in KNOWN_KEYS)
    if unknown:
        r.problems.append(f"unknown variable(s) {unknown}; check for typos")
    if r.problems:
        raise ConfigError(r.problems)


def load_runtime_settings(env: Mapping[str, str] | None = None) -> RuntimeSettings:
    r = _Reader(os.environ if env is None else env)
    settings = _read_runtime(r)
    _raise_if(r)
    return settings


def load_pipeline_settings(env: Mapping[str, str] | None = None) -> PipelineSettings:
    r = _Reader(os.environ if env is None else env)
    runtime = _read_runtime(r)

    country = r.pattern("IRIS_COUNTRY_CODE", COUNTRY_CODE_RE, "use an upper-case ISO 3166-1 alpha-2 code, e.g. 'FR'")
    region = r.pattern("IRIS_REGION_CODE", REGION_CODE_RE, "use the upper-case ISO 3166-2 subdivision part, e.g. 'IDF'")

    endpoint = r.required("IRIS_SOURCE_ENDPOINT")
    if endpoint:
        try:
            endpoint = resolve_source_endpoint(endpoint, country, region)
        except ValueError as exc:
            r.problems.append(f"IRIS_SOURCE_ENDPOINT: {exc}")
        else:
            _check_endpoint(r, endpoint)

    output_dir = r.required("IRIS_OUTPUT_DIR")
    # POSIX form is accepted everywhere: the value usually describes a path
    # inside the Linux container, even when the CLI is run from another OS.
    if output_dir and not (Path(output_dir).is_absolute() or PurePosixPath(output_dir).is_absolute()):
        r.problems.append(f"IRIS_OUTPUT_DIR={output_dir!r} must be an absolute path")

    settings = PipelineSettings(
        runtime=runtime,
        country_code=country,
        region_code=region,
        source_endpoint=endpoint,
        output_dir=Path(output_dir),
        eco_points_per_m2=r.number("IRIS_ECO_POINTS_PER_M2", 8.0, minimum=0, exclusive_minimum=True),
        max_reject_ratio=r.number("IRIS_MAX_REJECT_RATIO", 0.5, minimum=0, maximum=1),
        source_timeout_s=r.integer("IRIS_SOURCE_TIMEOUT_S", 30, minimum=1, maximum=600),
        source_max_bytes=r.integer("IRIS_SOURCE_MAX_BYTES", 50_000_000, minimum=1),
        run_interval_s=r.integer("IRIS_RUN_INTERVAL_S", 0, minimum=0),
    )
    _raise_if(r)
    return settings
