from __future__ import annotations

import json
import os

import pytest

from iris_pilot.config import (
    ConfigError,
    load_pipeline_settings,
    load_runtime_settings,
    redact_uri,
)

OUTPUT_DIR = os.path.abspath("/srv/iris-out")


def base_env(**overrides: str) -> dict[str, str]:
    env = {
        "IRIS_DB_HOST": "db.internal",
        "IRIS_DB_NAME": "iris",
        "IRIS_DB_USER": "iris",
        "IRIS_DB_PASSWORD": "s3cret-value",
        "IRIS_COUNTRY_CODE": "FR",
        "IRIS_REGION_CODE": "IDF",
        "IRIS_SOURCE_ENDPOINT": "https://feeds.example.org/{country_code}/{region_code}/sites.geojson",
        "IRIS_OUTPUT_DIR": OUTPUT_DIR,
    }
    env.update(overrides)
    return env


def problems_of(env: dict[str, str]) -> list[str]:
    with pytest.raises(ConfigError) as info:
        load_pipeline_settings(env)
    return info.value.problems


@pytest.mark.parametrize("country,region", [("FR", "IDF"), ("DE", "NW"), ("AT", "9"), ("US", "CA"), ("PL", "14")])
def test_any_scope_is_pure_configuration(country: str, region: str) -> None:
    s = load_pipeline_settings(base_env(IRIS_COUNTRY_CODE=country, IRIS_REGION_CODE=region))
    assert s.scope == f"{country}-{region}"
    assert s.source_endpoint == f"https://feeds.example.org/{country}/{region}/sites.geojson"
    assert s.scope_output_dir.name == f"{country}-{region}"


def test_defaults_for_tunables_only() -> None:
    s = load_pipeline_settings(base_env())
    assert s.db.port == 5432
    assert s.eco_points_per_m2 == 8.0
    assert s.max_reject_ratio == 0.5
    assert s.run_interval_s == 0
    assert s.runtime.log_level == "INFO"


def test_all_missing_values_are_reported_together() -> None:
    problems = problems_of({})
    joined = "\n".join(problems)
    for key in ("IRIS_DB_HOST", "IRIS_DB_NAME", "IRIS_DB_USER", "IRIS_DB_PASSWORD", "IRIS_COUNTRY_CODE",
                "IRIS_REGION_CODE", "IRIS_SOURCE_ENDPOINT", "IRIS_OUTPUT_DIR"):
        assert key in joined
    assert len(problems) == 8


@pytest.mark.parametrize("key,value", [
    ("IRIS_COUNTRY_CODE", "de"),
    ("IRIS_COUNTRY_CODE", "DEU"),
    ("IRIS_REGION_CODE", "nw"),
    ("IRIS_REGION_CODE", "NRW1"),
    ("IRIS_REGION_CODE", "N-W"),
])
def test_scope_codes_are_strict(key: str, value: str) -> None:
    assert any(key in p for p in problems_of(base_env(**{key: value})))


def test_empty_value_counts_as_missing() -> None:
    assert any("IRIS_COUNTRY_CODE is required" in p for p in problems_of(base_env(IRIS_COUNTRY_CODE="  ")))


def test_unknown_iris_variable_is_rejected() -> None:
    problems = problems_of(base_env(IRIS_COUNTY_CODE="FR"))
    assert any("IRIS_COUNTY_CODE" in p for p in problems)


def test_compose_only_and_foreign_variables_are_tolerated() -> None:
    load_pipeline_settings(base_env(IRIS_UID="1000", IRIS_OUTPUT_MOUNT="/srv/out", PATH="/usr/bin", HOME="/root"))


@pytest.mark.parametrize("raw,expected", [
    (b"from-file \n\n", "from-file "),
    (b"from-file", "from-file"),
    # Same as `$(< file)` in the postgres entrypoint: only LF is stripped.
    (b"from-file\r\n", "from-file\r"),
])
def test_password_file_matches_postgres_image_semantics(tmp_path, raw, expected) -> None:
    secret = tmp_path / "pw"
    secret.write_bytes(raw)
    env = base_env(IRIS_DB_PASSWORD_FILE=str(secret))
    del env["IRIS_DB_PASSWORD"]
    assert load_pipeline_settings(env).db.password.reveal() == expected


def test_password_sources_are_exclusive(tmp_path) -> None:
    secret = tmp_path / "pw"
    secret.write_text("x", encoding="utf-8")
    assert any("only one" in p for p in problems_of(base_env(IRIS_DB_PASSWORD_FILE=str(secret))))


@pytest.mark.parametrize("content", [None, ""])
def test_unreadable_or_empty_password_file(tmp_path, content) -> None:
    secret = tmp_path / "pw"
    if content is not None:
        secret.write_text(content, encoding="utf-8")
    env = base_env(IRIS_DB_PASSWORD_FILE=str(secret))
    del env["IRIS_DB_PASSWORD"]
    assert any("IRIS_DB_PASSWORD_FILE" in p for p in problems_of(env))


def test_secrets_never_render(tmp_path) -> None:
    s = load_pipeline_settings(base_env(
        IRIS_SOURCE_ENDPOINT="https://user:tok3n@feeds.example.org/x.geojson?api_key=abc123"))
    rendered = " ".join([repr(s), str(s), json.dumps(s.redacted()), repr(s.db), str(s.db.password)])
    for leaked in ("s3cret-value", "tok3n", "abc123"):
        assert leaked not in rendered


@pytest.mark.parametrize("endpoint,fragment", [
    ("ftp://feeds.example.org/sites.geojson", "scheme"),
    ("https:///sites.geojson", "host"),
    ("file://relative/sites.geojson", "absolute"),
    ("https://feeds.example.org/{country}/sites.geojson", "placeholder"),
    ("https://feeds.example.org/{country_code/sites.geojson", "braces"),
])
def test_invalid_endpoints(endpoint: str, fragment: str) -> None:
    assert any(fragment in p for p in problems_of(base_env(IRIS_SOURCE_ENDPOINT=endpoint)))


def test_file_endpoint_template() -> None:
    s = load_pipeline_settings(base_env(IRIS_SOURCE_ENDPOINT="file:///data/source/{country_code}-{region_code}/s.geojson"))
    assert s.source_endpoint == "file:///data/source/FR-IDF/s.geojson"


def test_output_dir_must_be_absolute() -> None:
    assert any("IRIS_OUTPUT_DIR" in p for p in problems_of(base_env(IRIS_OUTPUT_DIR="output")))


@pytest.mark.parametrize("key,value", [
    ("IRIS_DB_PORT", "70000"),
    ("IRIS_DB_PORT", "abc"),
    ("IRIS_MAX_REJECT_RATIO", "1.5"),
    ("IRIS_MAX_REJECT_RATIO", "nan"),
    ("IRIS_ECO_POINTS_PER_M2", "0"),
    ("IRIS_RUN_INTERVAL_S", "-1"),
    ("IRIS_LOG_LEVEL", "LOUD"),
    ("IRIS_DB_SSLMODE", "sometimes"),
])
def test_value_bounds(key: str, value: str) -> None:
    assert any(key in p for p in problems_of(base_env(**{key: value})))


def test_runtime_settings_need_only_database_values() -> None:
    env = {k: v for k, v in base_env().items() if k.startswith("IRIS_DB_")}
    settings = load_runtime_settings(env)
    assert settings.db.host == "db.internal"


def test_redact_uri() -> None:
    assert redact_uri("https://u:p@h.example/x?token=1#frag") == "https://***@h.example/x?***"
    assert redact_uri("file:///data/source/x.geojson") == "file:///data/source/x.geojson"
