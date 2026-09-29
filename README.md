# IRIS pilot runtime (IRIS-CAND-09)

A Docker Compose runtime for the IRIS pilot: PostgreSQL/PostGIS, a one-shot
migration job and a Python worker that ingests candidate sites for one
country/region scope, validates them against an explicit data contract,
stores them in PostGIS and writes deterministic GeoJSON outputs.

Country, region, source endpoint, database, credentials and output location
are configuration. The same image runs `DE/NW` on a workstation and `AT/9`
on a second host; only the env file differs.

```
docker compose ── db ─────── PostGIS 16 / 3.4, healthcheck, internal network only
                 ├─ migrate ─ ordered SQL migrations (checksummed, advisory-locked), then exits
                 ├─ worker ── fetch → validate → promote → export, for the configured scope
                 └─ source ── optional mock HTTP endpoint serving ./fixtures (profile mock-source)
```

---

## Quick start (smoke path)

Requirements: see [Host assumptions](#host-assumptions). From a clean checkout:

```sh
# Linux / macOS / WSL / Git Bash
sh scripts/smoke.sh

# Windows PowerShell
.\scripts\smoke.ps1

# or, where make is available
make smoke
```

The script:

1. checks Docker and Compose are available;
2. creates `.env` from `deploy/dev.env.example` if there is none;
3. generates a random database password into `secrets/db_password` if missing;
4. validates the Compose configuration (missing required variables fail here);
5. builds the image, starts `db` (and the mock `source`) and waits until healthy;
6. runs the migration job;
7. runs `worker smoke`, which prints one line per check:

```
PASS  configuration loads: scope=DE-NW source=http://source:8080/DE-NW/sites.geojson output=/data/output/DE-NW
PASS  database reachable, versions supported: server_version_num=160xxx postgis=3.4.x
PASS  schema at head: no pending migrations
PASS  worker run succeeds: run_id=1 accepted=4 rejected=2
PASS  database invariants: 4 site(s) valid in EPSG:4326, 2 rejection(s) recorded
PASS  output files: sites.geojson, run-summary.json
PASS  rerun is idempotent: sha256=… unchanged
SMOKE OK
```

The two rejections are intentional: the fixture contains a self-intersecting
polygon and a feature without geometry.

Stop the stack with `docker compose down`; add `--volumes` to also delete the
database and output volumes.

---

## Host assumptions

| Area | Assumption |
|---|---|
| Container runtime | Docker Engine 24+ (or Docker Desktop) with the Compose v2 plugin **2.24 or newer** (`depends_on.required`, `up --wait-timeout`). |
| CPU architecture | `linux/amd64` or `linux/arm64`. The worker image and its wheels are multi-arch. The default `postgis/postgis` image is amd64-only; on arm64 hosts set `IRIS_POSTGIS_IMAGE=imresamu/postgis:16-3.4` (multi-arch build of the same image). CI runs the arm64 path this way. |
| Network | Internet access for the first build (base images, pinned PyPI wheels). Runtime needs no internet with the bundled fixtures. |
| Ports | None published on the host, so nothing collides with local services. |
| Working directory | Commands run from the repository root; relative paths (`./fixtures`, `./secrets/…`) resolve against it. |
| Disk | About 1 GB for images and volumes. |
| Line endings | `.gitattributes` forces LF for everything executed in containers, so Windows checkouts behave the same. |
| SELinux | On enforcing hosts, bind mounts may need a `:z` suffix (host policy, not handled in the Compose file). |
| PostgreSQL major | The volume mount matches the 16/17 images. PostgreSQL 18 images moved the data directory and need a different mount. |

---

## Commands

All commands accept `--env-file <file>`; without it Compose reads `.env`.

| Task | Command |
|---|---|
| Smoke path | `sh scripts/smoke.sh [ENV_FILE]` / `.\scripts\smoke.ps1 [-EnvFile ENV_FILE]` |
| Normal run (db → migrate → worker) | `docker compose up --build` |
| Another ingest run | `docker compose run --rm worker run` |
| Effective configuration, secrets redacted | `docker compose run --rm --no-deps worker config` |
| Schema status without changing it | `docker compose run --rm migrate migrate --check` |
| Read an output file | `docker compose run --rm --no-deps --entrypoint cat worker /data/output/DE-NW/run-summary.json` |
| Tests inside the stack (unit + integration) | `docker compose --profile test run --rm --build tests` |
| Tests on the host | `pip install -e ".[test]"` then `pytest` (integration tests run when `IRIS_DB_*` point at a PostGIS server, otherwise they are skipped) |
| Stop / wipe | `docker compose down` / `docker compose down --volumes` |

Worker exit codes: `0` ok, `1` run or check failed, `2` configuration error,
`3` dependency unavailable (database unreachable, schema behind or drifted).

---

## Changing scope or host

### Another country/region (acceptance criterion 1)

Edit two lines of the env file and point the source at that region's data:

```ini
IRIS_COUNTRY_CODE=FR
IRIS_REGION_CODE=IDF
IRIS_SOURCE_ENDPOINT=https://feeds.example.org/{country_code}/{region_code}/sites.geojson
```

`{country_code}` and `{region_code}` are expanded by the worker, so a
templated endpoint often needs no change at all. No code, SQL, image or
Compose edit is involved: all tables are keyed by `country_code`, outputs go
to `<IRIS_OUTPUT_DIR>/<country>-<region>/`, and a source that declares a
different scope is refused rather than loaded.

### Second host

`deploy/second-host.env.example` is a complete example for another machine:
scope `AT/9`, a file-based source mounted from the host instead of the mock
HTTP service, a separate Compose project, database name and password file,
and a long-running worker that re-ingests every 6 hours.

```sh
cp deploy/second-host.env.example deploy/second-host.env
# adjust IRIS_SOURCE_MOUNT / IRIS_DB_SECRET_FILE / IRIS_OUTPUT_MOUNT for the host
sh scripts/smoke.sh deploy/second-host.env
docker compose --env-file deploy/second-host.env up -d
```

Because the project name differs, both stacks can also share one Docker host.

---

## Configuration reference

Variables are read from the env file by Compose. The worker only sees what
`docker-compose.yml` passes explicitly; there is no `env_file:` that would
forward everything.

**Scope and source**

| Variable | Default | Meaning |
|---|---|---|
| `IRIS_COUNTRY_CODE` | required | ISO 3166-1 alpha-2, upper case. |
| `IRIS_REGION_CODE` | required | ISO 3166-2 subdivision part, upper case, 1–3 characters. |
| `IRIS_SOURCE_ENDPOINT` | required | `http(s)://…` or `file:///…`; may contain `{country_code}` / `{region_code}`. |
| `IRIS_SOURCE_TIMEOUT_S` | `30` | HTTP timeout per attempt (3 attempts, 5xx and network errors only). |
| `IRIS_SOURCE_MAX_BYTES` | `50000000` | Refuse larger source documents. |
| `IRIS_SOURCE_MOUNT` | `./fixtures` | Host directory mounted read-only at `/data/source` for `file://` sources. |

**Database**

| Variable | Default | Meaning |
|---|---|---|
| `IRIS_DB_NAME`, `IRIS_DB_USER` | required | Database and role created by the PostGIS container on first start. |
| `IRIS_DB_SECRET_FILE` | `./secrets/db_password` | Host file holding the password, mounted as a Compose secret. |
| `IRIS_DB_SSLMODE` | `prefer` | libpq `sslmode`, for an external server. |
| `IRIS_POSTGIS_IMAGE` | `postgis/postgis:16-3.4` | Database image. |

Inside containers the worker uses `IRIS_DB_HOST=db`, `IRIS_DB_PORT=5432` and
`IRIS_DB_PASSWORD_FILE=/run/secrets/db_password`. Outside Compose,
`IRIS_DB_PASSWORD` is accepted as an alternative (never both).

**Output, screening and process**

| Variable | Default | Meaning |
|---|---|---|
| `IRIS_OUTPUT_DIR` | `/data/output` | Output path inside the container. |
| `IRIS_OUTPUT_MOUNT` | `output` | What is mounted there: the named volume, or a host path. |
| `IRIS_UID`, `IRIS_GID` | `10001` | Container user; set to the owner of a host output directory. |
| `IRIS_ECO_POINTS_PER_M2` | `8` | Commercial baseline factor used for indicative eco-points. |
| `IRIS_MAX_REJECT_RATIO` | `0.5` | Above this share of rejected features the run fails and existing data is kept. |
| `IRIS_RUN_INTERVAL_S` | `0` | `0` runs once; `N` keeps the worker running and re-ingests every N seconds. |
| `IRIS_WORKER_RESTART` | `no` | Compose restart policy for the worker. |
| `IRIS_STARTUP_TIMEOUT_S` | `60` | How long to wait for the database and migrations. |
| `IRIS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `IRIS_IMAGE_TAG` | `local` | Tag for the locally built image. |
| `COMPOSE_PROJECT_NAME` | `iris` | Isolates containers, networks and volumes per deployment. |
| `COMPOSE_PROFILES` | – | `mock-source` starts the local HTTP source. |

Configuration is validated strictly: every problem is reported in one go,
values are range-checked, and an unknown `IRIS_*` variable (for example the
typo `IRIS_COUNTY_CODE`) is an error instead of being silently ignored.

---

## Architecture and design choices

### Startup ordering (acceptance criterion 3)

* `db` has a healthcheck that runs `pg_isready` **and** a real query over
  TCP. During the image's init phase PostgreSQL only listens on its Unix
  socket, so the check cannot pass before the final server is up.
* `migrate` depends on `db: service_healthy`; `worker` depends on
  `db: service_healthy` and `migrate: service_completed_successfully`.
* The worker does not rely on the orchestrator alone. It retries the
  connection with capped backoff up to `IRIS_STARTUP_TIMEOUT_S`, fails fast on
  permanent errors (wrong password, unknown database), and refuses to run
  until the schema matches its own migration set. The same holds when it is
  started with `docker compose run` or outside Compose.

### Deterministic migrations

* Plain SQL files in `src/iris_pilot/migrations/NNNN_name.sql`, shipped inside
  the image. Versions must be contiguous from `0001`; order never depends on
  directory listing.
* Each file is applied in its own transaction together with its ledger row in
  `public.schema_migration`, so a failing migration leaves no partial state.
* The ledger stores a SHA-256 of each file (line endings normalised). An
  edited, already-applied migration, or a database that is ahead of the image,
  stops the job with an explicit error.
* A PostgreSQL advisory lock serialises concurrent migrators.
* Migration `0001` asserts PostgreSQL ≥ 16 and PostGIS ≥ 3.4.

### Data contract and processing

Source format: a GeoJSON `FeatureCollection` with `source_date` (required),
optional `country_code`/`region_code` (must match the deployment) and an
optional named `crs` (absent means RFC 7946 WGS84). Features need
`properties.site_id` and a Polygon/MultiPolygon; `name`, a per-feature
`source_date` and `positional_accuracy_m` are optional.

A run is:

1. **fetch** from the configured endpoint (size limit, retries for transient errors only);
2. **contract checks** in Python: structure, closed rings, finite coordinates,
   dates, scope, duplicate identifiers (every copy rejected, none chosen silently);
3. **geometry checks** in PostGIS: `ST_IsValid` with the GEOS reason, empty
   geometries, and bounds after transformation to EPSG:4326. Invalid
   geometries are rejected, never repaired;
4. **reject-ratio gate**: if nothing is valid or the rejected share exceeds
   `IRIS_MAX_REJECT_RATIO`, the run fails and the scope's current data stays;
5. **promote** in one transaction: upsert changed sites, remove sites no longer
   in the source, record rejections with reasons, update the run row;
6. **export** `sites.geojson` and `run-summary.json` via temp file + rename;
7. mark the run `succeeded` only after database and files agree. Any failure
   marks it `failed` with the error.

A per-scope advisory lock prevents two workers from processing the same scope
at once.

Explicit contracts rather than defaults:

| Aspect | How it is handled |
|---|---|
| CRS | Declared or RFC 7946 default; unknown or unrecognised CRS refused; stored as `geometry(MultiPolygon, 4326)` in the canonical column `geom`. |
| Units | `area_m2` is geodesic area on the WGS84 spheroid (`ST_Area(geom::geography)`), independent of any national projection. |
| Source date | Required at collection level, optionally per feature; persisted per site and per run. |
| Uncertainty | `positional_accuracy_m` is stored when given and stays `NULL` (unknown) when not. Outputs carry the project disclaimer and label eco-points as indicative. |
| Completeness | Every rejected feature is persisted with a reason; the run summary counts known/unknown optional attributes. |

### Data model

| Table | Key | Notes |
|---|---|---|
| `iris.ingest_run` | `(country_code, run_id)` | Audit of every run: redacted source URI, source SHA-256, source date and CRS, counts, status, error. |
| `iris.site_candidate` | `(country_code, region_code, site_id)` | Current sites per scope. FK `(country_code, ingest_run_id)` → run. Checks for code format, valid/non-empty geometry, positive area. GiST index on `geom`. |
| `iris.ingest_rejection` | `(country_code, ingest_run_id, feature_index)` | Features that failed the contract, with reason and detail. |

Every business table has a non-null, format-checked `country_code`, and every
foreign key includes it, so rows of different countries cannot be joined by
accident. The same `site_id` in two scopes is two different sites.

### Outputs

Written to `<IRIS_OUTPUT_DIR>/<country>-<region>/`:

* `sites.geojson`: accepted sites with scope, `area_m2`,
  `eco_points_indicative`, `positional_accuracy_m`, source date, units metadata
  and the disclaimer. Byte-identical for identical database state (sorted keys,
  `C`-collation ordering, fixed coordinate precision, no timestamps), which the
  smoke test verifies by checksum.
* `run-summary.json`: run id, timestamps, source checksum, counts,
  rejections with reasons, completeness, output checksums and the redacted
  effective configuration.

### Security basics (acceptance criterion 2)

* **No secrets in images.** The build context is an allow-list
  (`.dockerignore` starts with `**`), so `.env`, `secrets/` and outputs cannot
  enter it. The Dockerfile sets no credentials; a test checks both.
* **Password as a secret file.** Mounted at `/run/secrets/db_password` for
  PostgreSQL (`POSTGRES_PASSWORD_FILE`) and the worker
  (`IRIS_DB_PASSWORD_FILE`), not passed as an environment variable, so it does
  not show up in `docker inspect`. The smoke script generates a random one per
  host; templates contain no password.
* **Redaction.** Credentials and query strings in endpoints are redacted in
  logs, the run table and summaries; the settings objects never render the
  password.
* **Least privilege at runtime.** Non-root user, read-only root filesystem,
  all Linux capabilities dropped, `no-new-privileges`, `tmpfs` for `/tmp`.
* **Network.** The database is on an internal network with no published port
  and no route out. Only the worker also joins the egress network.
* **Supply chain.** Base image pinned by tag; Python dependencies pinned with
  hashes and installed from wheels only (`--require-hashes --only-binary`).

### Why these tools

* **Plain SQL migrations with a small runner** instead of Alembic: the schema
  is PostGIS-heavy DDL, a few lines of runner give checksums and locking, and
  the image stays at one runtime dependency (`psycopg`).
* **Standard library for config, HTTP and logging**: fewer moving parts in a
  runtime whose main goal is portability.
* **Validation in PostGIS** rather than a Python geometry library: GEOS
  through PostGIS is the same engine that later answers spatial queries, so
  "valid" means the same thing everywhere.

---

## Tests

`pytest` covers the highest-risk conditions:

| Area | Examples |
|---|---|
| Configuration | any scope loads from env alone; all problems reported together; strict codes; unknown variables rejected; secret file semantics match the postgres image; secrets and endpoint tokens never rendered |
| Deployment files | each env template, interpolated through the real `docker-compose.yml`, yields valid worker settings; no scope literal in code, SQL, Compose or Dockerfile; password only via secret file; startup ordering; hardening; allow-list build context |
| Data contract | fixtures; CRS parsing; scope mismatch; required source date; duplicates; malformed geometry; attribute types |
| Migrations (unit + DB) | ordering, gaps, duplicates, CRLF-stable checksums; idempotent apply; drift and "database ahead" detection; failed migration rolls back |
| Pipeline (DB) | promotion and rejections; idempotent rerun; scope isolation with equal site ids; removal of vanished sites; reject-ratio gate keeps previous data; contract and source failures recorded; EPSG:3857 input transformed; unknown SRID refused; concurrent run refused; smoke for both scopes |
| Runtime | database retry and fail-fast; HTTP retry policy and size limit; atomic, deterministic outputs; CLI exit codes |

Integration tests create and drop a temporary database per test, so they
never touch pilot data.

`.github/workflows/docker-smoke.yml` runs the documented smoke path on clean
GitHub-hosted runners for both env templates (amd64) and the dev template on
arm64, checks the built image for leaked secrets, verifies the plain
`docker compose up` ordering, and runs the test suite inside the stack.

---

## Fixtures

`fixtures/<country>-<region>/sites.geojson` holds small synthetic data for
`DE-NW` and `AT-9`. Both deliberately include features that must be rejected
(self-intersection, missing geometry, missing identifier, foreign scope) so the
rejection path is exercised on every smoke run. They are not real parcels.

---

## Deliberate simplifications and the production path

| Simplification | Production evolution |
|---|---|
| The application connects as the database owner created by the image (needed for `CREATE EXTENSION`). | Separate roles: a migration owner and a worker role with DML rights on `iris` only; extensions pre-provisioned by the platform team. |
| Password in a host file generated by the smoke script. | Secrets from a manager (Vault, cloud secret store, Swarm/Kubernetes secrets), rotated; TLS to the database with `IRIS_DB_SSLMODE=verify-full`. |
| PostGIS runs in the stack. | Managed PostgreSQL/PostGIS: an override file removes the `db` service, sets `IRIS_DB_HOST`/`IRIS_DB_SSLMODE` and gives the worker a routable network. |
| One source format (GeoJSON sites). | Adapter registry per source type, selected by configuration, each with its own contract tests. |
| Full refresh per scope with a reject-ratio gate. | Staged promotion (staging tables, diff report, manual or rule-based approval) and history tables for audit. |
| Region is a code only. | Region boundary per scope and a spatial check that sites fall inside it. |
| Built locally, tags only. | CI builds multi-arch images, scans them, signs them, pins base images by digest and promotes by immutable tag. |
| Logs in logfmt on stdout. | Shipped to central logging, metrics per run (duration, counts, reject ratio) and alerting on failed runs. |
| Loop mode for scheduling. | External scheduler (cron, Kubernetes CronJob, workflow engine) running one-shot containers. |

---

## Repository layout

```
docker-compose.yml         stack definition (db, migrate, worker, mock source, tests)
Dockerfile                 multi-stage: deps → runtime (non-root) → test
deploy/                    dev.env.example, second-host.env.example
scripts/                   smoke.sh, smoke.ps1
requirements/              hash-pinned runtime and test locks for the image
src/iris_pilot/
  config.py                configuration module (strict, redacting)
  db.py                    connections with bounded startup retries
  migrate.py               migration runner; migrations/*.sql
  sources.py               http(s)/file source adapter
  contract.py              source data contract
  store.py                 SQL for runs, validation, promotion
  pipeline.py              one run: fetch → validate → promote → export
  export.py                deterministic outputs, disclaimer
  smoke.py                 end-to-end self-check
  cli.py                   config | migrate | run | smoke | healthcheck
fixtures/                  synthetic DE-NW and AT-9 sources
tests/                     unit, deployment and integration tests
```
