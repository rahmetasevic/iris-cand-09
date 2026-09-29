"""Static guards on the deployment artefacts.

These encode the acceptance criteria that are about files rather than
behaviour: no hard-coded scope, no secrets in images or templates, and
startup ordering that waits for a healthy database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from conftest import REPO_ROOT
from iris_pilot.config import DEPLOYMENT_KEYS, PIPELINE_KEYS, RUNTIME_KEYS, load_pipeline_settings

TEMPLATES = sorted((REPO_ROOT / "deploy").glob("*.env.example"))
APP_SERVICES = ("migrate", "worker", "source", "tests")


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_there_are_dev_and_second_host_templates() -> None:
    assert {p.name for p in TEMPLATES} >= {"dev.env.example", "second-host.env.example"}


def test_templates_target_different_scopes() -> None:
    scopes = {(read_env_file(p)["IRIS_COUNTRY_CODE"], read_env_file(p)["IRIS_REGION_CODE"]) for p in TEMPLATES}
    assert len(scopes) == len(TEMPLATES)


_VAR_RE = re.compile(r"\$\$|\$\{(?P<name>[A-Z0-9_]+)(?:(?P<op>:-|:\?)(?P<arg>[^}]*))?\}")


def interpolate(value: str, env: dict[str, str]) -> str:
    """The subset of compose interpolation used in docker-compose.yml."""
    def replace(match: re.Match) -> str:
        if match.group(0) == "$$":
            return "$"
        current = env.get(match["name"], "")
        if current:
            return current
        if match["op"] == ":?":
            raise AssertionError(f"compose would refuse to start: {match['arg']}")
        return match["arg"] or ""
    return _VAR_RE.sub(replace, value)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_template_yields_valid_worker_settings(template: Path, compose: dict, tmp_path: Path) -> None:
    env = read_env_file(template)
    unknown = {k for k in env if k.startswith("IRIS_")} - (RUNTIME_KEYS | PIPELINE_KEYS | DEPLOYMENT_KEYS)
    assert not unknown, f"{template.name} sets unknown variables {unknown}"

    worker_env = {k: interpolate(str(v), env) for k, v in compose["services"]["worker"]["environment"].items()}
    # Stand-in for the mounted secret; everything else is exactly what compose passes.
    secret = tmp_path / "db_password"
    secret.write_text("placeholder", encoding="utf-8")
    worker_env["IRIS_DB_PASSWORD_FILE"] = str(secret)

    settings = load_pipeline_settings(worker_env)
    assert settings.scope == f"{env['IRIS_COUNTRY_CODE']}-{env['IRIS_REGION_CODE']}"
    mounts = [interpolate(v, env) for v in compose["services"]["worker"]["volumes"]]
    assert f"{settings.output_dir.as_posix()}" in {m.split(":")[-1] for m in mounts}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_templates_hold_no_secrets(template: Path) -> None:
    for key, value in read_env_file(template).items():
        if re.search(r"PASSWORD|SECRET|TOKEN|KEY", key) and not key.endswith("_FILE"):
            pytest.fail(f"{template.name}: {key} must not carry a value in a template")
        assert not re.search(r"://[^/\s]*:[^/\s]*@", value), f"{template.name}: credentials in {key}"


@pytest.mark.parametrize("scope", sorted({
    (read_env_file(p)["IRIS_COUNTRY_CODE"], read_env_file(p)["IRIS_REGION_CODE"]) for p in TEMPLATES
}))
def test_no_scope_is_hard_coded(scope: tuple[str, str]) -> None:
    country, region = scope
    patterns = [re.compile(rf"['\"]{country}['\"]"), re.compile(rf"['\"]{region}['\"]"),
                re.compile(rf"\b{country}-{region}\b"), re.compile(rf"\b{country}/{region}\b")]
    files = [*(REPO_ROOT / "src").rglob("*.py"), *(REPO_ROOT / "src").rglob("*.sql"),
             REPO_ROOT / "docker-compose.yml", REPO_ROOT / "Dockerfile"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            assert not pattern.search(text), f"{path.relative_to(REPO_ROOT)} hard-codes {pattern.pattern}"


def test_worker_receives_every_pipeline_setting(compose: dict) -> None:
    env = compose["services"]["worker"]["environment"]
    assert PIPELINE_KEYS <= set(env)
    assert RUNTIME_KEYS - {"IRIS_DB_PASSWORD", "IRIS_DB_CONNECT_TIMEOUT_S"} <= set(env)


def test_database_password_only_travels_as_a_secret_file(compose: dict) -> None:
    for name, service in compose["services"].items():
        assert "env_file" not in service, f"{name}: env_file would forward every variable"
        env = service.get("environment", {})
        assert "IRIS_DB_PASSWORD" not in env and "POSTGRES_PASSWORD" not in env, name
        if "IRIS_DB_PASSWORD_FILE" in env or "POSTGRES_PASSWORD_FILE" in env:
            assert "db_password" in service.get("secrets", []), name
    assert "file" in compose["secrets"]["db_password"]


def test_database_is_healthy_before_anything_runs(compose: dict) -> None:
    services = compose["services"]
    assert services["db"]["healthcheck"]["test"]
    assert services["migrate"]["depends_on"]["db"]["condition"] == "service_healthy"
    worker = services["worker"]["depends_on"]
    assert worker["db"]["condition"] == "service_healthy"
    assert worker["migrate"]["condition"] == "service_completed_successfully"


def test_database_is_not_exposed(compose: dict) -> None:
    db = compose["services"]["db"]
    assert "ports" not in db
    assert db["networks"] == ["backend"]
    assert compose["networks"]["backend"]["internal"] is True


@pytest.mark.parametrize("name", APP_SERVICES)
def test_app_containers_are_hardened(compose: dict, name: str) -> None:
    service = compose["services"][name]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert not str(service["user"]).startswith("0")


def test_image_bakes_in_no_configuration_or_secrets() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for line in dockerfile.splitlines():
        if re.match(r"\s*(ENV|ARG)\b", line):
            assert not re.search(r"PASSWORD|SECRET|TOKEN|IRIS_", line, re.I), line
    users = re.findall(r"^USER\s+(\S+)", dockerfile, re.M)
    assert users and users[-1] != "root" and not users[-1].startswith("0")


def test_build_context_is_an_allow_list() -> None:
    rules = [line.strip() for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    assert rules[0] == "**"
    reincluded = [r[1:] for r in rules if r.startswith("!")]
    for forbidden in (".env", "secrets", "output"):
        assert not any(r.startswith(forbidden) for r in reincluded), forbidden
