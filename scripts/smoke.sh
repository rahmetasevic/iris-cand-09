#!/bin/sh
# Documented smoke path for a clean Docker host.
#
#   scripts/smoke.sh [ENV_FILE]      default: .env (created from deploy/dev.env.example)
#
# Steps: preflight -> secret -> compose config -> build -> db (+ mock source)
# healthy -> migrate -> worker smoke checks. Exits non-zero on the first failure.
set -eu

cd "$(dirname "$0")/.."

ENV_FILE=${1:-.env}
WAIT_TIMEOUT=${SMOKE_WAIT_TIMEOUT:-180}

log() { printf '[smoke] %s\n' "$*"; }
die() { printf '[smoke] ERROR: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running, is this user allowed?)"
docker compose version >/dev/null 2>&1 || die "the Docker Compose v2 plugin is required ('docker compose')"
log "using $(docker compose version)"

if [ ! -f "$ENV_FILE" ]; then
    [ "$ENV_FILE" = ".env" ] || die "env file not found: $ENV_FILE"
    cp deploy/dev.env.example .env
    log "created .env from deploy/dev.env.example"
fi

# Last assignment wins, like compose; surrounding quotes are stripped.
env_value() {
    sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -n 1 | sed "s/^[\"']//; s/[\"']$//"
}

SECRET_FILE=$(env_value IRIS_DB_SECRET_FILE)
SECRET_FILE=${SECRET_FILE:-./secrets/db_password}
if [ ! -s "$SECRET_FILE" ]; then
    secret_dir=$(dirname "$SECRET_FILE")
    mkdir -p "$secret_dir"
    chmod 700 "$secret_dir" 2>/dev/null || true
    # 0644 so the non-root container user can read it once bind-mounted; the
    # 0700 parent directory keeps other host users out.
    (umask 022 && LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 >"$SECRET_FILE")
    log "generated a random database password in $SECRET_FILE"
fi

dc() { docker compose --env-file "$ENV_FILE" "$@"; }

log "validating compose configuration"
dc config --quiet

log "building images"
dc build

services=db
if dc config --services | grep -qx source; then
    services="db source"
fi
log "starting: $services (waiting for healthy, up to ${WAIT_TIMEOUT}s)"
# shellcheck disable=SC2086
dc up --detach --wait --wait-timeout "$WAIT_TIMEOUT" $services

log "applying migrations"
dc run --rm --no-deps migrate

log "running worker smoke checks"
dc run --rm --no-deps worker smoke

log "OK - stack is running; stop it with: docker compose --env-file $ENV_FILE down"
