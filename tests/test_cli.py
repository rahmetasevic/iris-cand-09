from __future__ import annotations

import json

import pytest

from iris_pilot.cli import EXIT_CONFIG, EXIT_OK, main


@pytest.fixture
def clean_env(monkeypatch):
    import os
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key)
    return monkeypatch


def test_config_errors_exit_2_and_list_every_problem(clean_env, capsys) -> None:
    clean_env.setenv("IRIS_COUNTRY_CODE", "fr")
    assert main(["config"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "IRIS_DB_HOST is required" in err
    assert "IRIS_COUNTRY_CODE='fr' is invalid" in err


def test_config_prints_redacted_settings(clean_env, capsys, tmp_path) -> None:
    for key, value in {
        "IRIS_DB_HOST": "db", "IRIS_DB_NAME": "n", "IRIS_DB_USER": "u", "IRIS_DB_PASSWORD": "hunter2",
        "IRIS_COUNTRY_CODE": "FR", "IRIS_REGION_CODE": "IDF", "IRIS_OUTPUT_DIR": str(tmp_path),
        "IRIS_SOURCE_ENDPOINT": "https://feeds.example.org/{country_code}.geojson",
    }.items():
        clean_env.setenv(key, value)
    assert main(["config"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert json.loads(out)["source_endpoint"] == "https://feeds.example.org/FR.geojson"


def test_migrate_needs_only_database_settings(clean_env, capsys) -> None:
    # No scope variables at all: the failure must be about the database only.
    assert main(["migrate", "--check"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "IRIS_DB_HOST" in err and "IRIS_COUNTRY_CODE" not in err
