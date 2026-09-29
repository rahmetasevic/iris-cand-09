# IRIS pilot runtime (IRIS-CAND-09)

[![docker-smoke](https://github.com/rahmetasevic/iris-cand-09/actions/workflows/docker-smoke.yml/badge.svg)](https://github.com/rahmetasevic/iris-cand-09/actions/workflows/docker-smoke.yml)

PostgreSQL/PostGIS, a one-shot migration job and a Python worker that ingests
candidate sites for one country/region, validates them against an explicit
data contract and writes deterministic GeoJSON outputs. Country, region, source
endpoint, database, credentials and output path are configuration: the same
image runs `DE/NW` and `AT/9`, only the env file differs.

```
docker compose ── db ─────── PostGIS 16 / 3.4, healthcheck, internal network only
                 ├─ migrate ─ ordered, checksummed SQL migrations, then exits
                 ├─ worker ── fetch → validate → promote → export for the configured scope
                 └─ source ── optional mock HTTP endpoint serving ./fixtures (profile mock-source)
```

## Quick start

```sh
sh scripts/smoke.sh        # Linux, macOS, WSL, Git Bash
.\scripts\smoke.ps1        # Windows PowerShell
```

The script creates `.env` from `deploy/dev.env.example` and a random password
in `secrets/db_password` if they are missing, builds the image, waits for a
healthy database, runs the migrations and then `worker smoke`: seven
end-to-end checks (configuration, database versions, schema, a real run,
database invariants, output files, idempotent rerun) ending in `SMOKE OK`.
The fixtures contain two deliberately invalid features, so `rejected=2` is
expected.

Stop with `docker compose down`; `--volumes` also deletes data and outputs.

## Acceptance criteria

| Criterion | How it is met | Verified by |
|---|---|---|
| 1. Other country/region without code edits | Scope, source, database and output come from the env file; tables are keyed by `country_code` | `test_no_scope_is_hard_coded`, `test_template_yields_valid_worker_settings`; CI runs DE/NW and AT/9 |
| 2. No secrets baked into images | Allow-list `.dockerignore`; password mounted at runtime as a secret file | `test_image_bakes_in_no_configuration_or_secrets`; CI searches the built image for the generated password |
| 3. Database healthy before the worker runs | `depends_on` with `service_healthy` and `service_completed_successfully`; the worker also waits for the schema | `test_database_is_healthy_before_anything_runs`; CI checks the plain `docker compose up` path |
| 4. Clean host runs the smoke path | `scripts/smoke.sh`, `scripts/smoke.ps1` | CI on fresh amd64 and arm64 runners |

## Host assumptions

- Docker Engine 24+ or Docker Desktop with Compose **2.24+**, Linux containers.
- `linux/amd64` or `linux/arm64`. `postgis/postgis` is amd64-only; on arm64 set
  `IRIS_POSTGIS_IMAGE=imresamu/postgis:16-3.4`.
- Internet access for the first build only. No host ports are published.
- Commands run from the repository root (relative paths such as `./fixtures`).
- SELinux hosts may need `:z` on bind mounts; PostgreSQL 18 images need a
  different data mount.

## Commands

| Task | Command |
|---|---|
| Normal run (db → migrate → worker) | `docker compose up --build` |
| Another ingest run | `docker compose run --rm worker run` |
| Effective configuration, secrets redacted | `docker compose run --rm --no-deps worker config` |
| Read an output | `docker compose run --rm --no-deps --entrypoint cat worker /data/output/DE-NW/sites.geojson` |
| Tests inside the stack | `docker compose --profile test run --rm --build tests` |
| Tests on the host | `pip install -e ".[test]"`, then `pytest` (integration tests need `IRIS_DB_*`, otherwise skipped) |

Use `--env-file <file>` for another env file. Exit codes: `0` ok, `1` run
failed, `2` configuration error, `3` database or schema not available.

## Configuration

| | `deploy/dev.env.example` | `deploy/second-host.env.example` |
|---|---|---|
| Scope | `DE` / `NW` | `AT` / `9` |
| Source | mock HTTP service | `file://` from a mounted host folder |
| Database | `iris` | `iris_at`, own project and password file |
| Schedule | run once | every 6 h, restart `unless-stopped` |

Main variables; the rest have defaults and are commented in the templates:

| Variable | Purpose |
|---|---|
| `IRIS_COUNTRY_CODE`, `IRIS_REGION_CODE` | Scope, upper-case ISO 3166 codes |
| `IRIS_SOURCE_ENDPOINT` | `http(s)://` or `file:///`; `{country_code}` / `{region_code}` are expanded |
| `IRIS_DB_NAME`, `IRIS_DB_USER`, `IRIS_DB_SECRET_FILE` | Database, role and the host file holding the password |
| `IRIS_OUTPUT_DIR`, `IRIS_OUTPUT_MOUNT` | Output path in the container and the volume or host folder mounted there |
| `IRIS_ECO_POINTS_PER_M2`, `IRIS_MAX_REJECT_RATIO` | Eco-point factor (8) and the reject share above which a run fails (0.5) |
| `IRIS_RUN_INTERVAL_S` | `0` runs once, `N` re-ingests every N seconds |

Validation is strict: all problems are reported at once, and an unknown
`IRIS_*` variable (for example `IRIS_COUNTY_CODE`) is an error. For the second
host: `cp deploy/second-host.env.example deploy/second-host.env`, adjust the
paths, then `sh scripts/smoke.sh deploy/second-host.env`.

## Design choices

- **Startup order.** The `db` health check runs `pg_isready` and a query over
  TCP, which cannot pass during the image's socket-only init phase. `migrate`
  waits for it and `worker` waits for both. The worker also retries the
  connection within a time limit, fails fast on a wrong password and refuses
  to run until the schema matches its migrations.
- **Migrations.** SQL files `NNNN_name.sql` with contiguous versions, each
  applied in one transaction and recorded with a SHA-256 in
  `public.schema_migration`. An edited migration or a database ahead of the
  image stops the job; an advisory lock serialises migrators; `0001` requires
  PostgreSQL 16 and PostGIS 3.4.
- **Data contract.** GeoJSON with a required `source_date`, an optional
  declared CRS (default RFC 7946 WGS84) and a scope that must match. Features
  that fail, including invalid geometry per `ST_IsValid`, are rejected with a
  reason, never repaired or filled in; a missing accuracy stays `NULL`. Above
  the reject ratio the run fails and existing data is kept.
- **Country scoping.** `country_code` is part of every primary and foreign key
  in `iris.ingest_run`, `iris.site_candidate` and `iris.ingest_rejection`.
  Geometry is stored in `geom` as `MultiPolygon, 4326`; area is geodesic.
- **Runs and outputs.** Each scope is promoted in one transaction under an
  advisory lock. `<output>/<country>-<region>/sites.geojson` is written
  atomically and is byte-identical for identical data; `run-summary.json`
  records source checksum, counts, rejections and redacted settings. A run is
  marked `succeeded` only after both files are written.
- **Security.** The password is only a secret file (`/run/secrets/db_password`),
  never an environment variable or image layer. Allow-list `.dockerignore`;
  non-root, read-only containers with all capabilities dropped; the database on
  an internal network; credentials in URLs redacted; hash-pinned wheels.

## Tests

`pytest` covers configuration, the deployment files (no hard-coded scope,
secrets only as files, startup order), the data contract, and migrations and
runs against real PostGIS, each integration test in a throw-away database.
`.github/workflows/docker-smoke.yml` runs both smoke scripts on clean amd64 and
arm64 runners, checks the image for secrets and runs the tests in the stack.

## Simplifications and production path

| Now | For production |
|---|---|
| App connects as the database owner (needed for `CREATE EXTENSION`) | Separate migration and worker roles |
| Password file generated per host | Secret manager with rotation; TLS with `IRIS_DB_SSLMODE=verify-full` |
| PostGIS inside the stack | Managed PostgreSQL/PostGIS via an override file |
| One source format, full refresh per scope | Adapters per source type; staged promotion with diff report and history |
| Region is a code only | Region boundaries and a containment check |
| Local build, images pinned by tag | CI-built, scanned, signed images pinned by digest |
| Worker loop for scheduling, logs on stdout | External scheduler; central logs, run metrics and alerts |
