# Convenience wrappers; every target is a plain docker compose call.
ENV_FILE ?= .env
COMPOSE  := docker compose --env-file $(ENV_FILE)

.PHONY: smoke up run logs test config down clean

smoke:            ## build, start, migrate and run the end-to-end smoke check
	sh scripts/smoke.sh $(ENV_FILE)

up:               ## start the stack; the worker runs once (or loops if IRIS_RUN_INTERVAL_S > 0)
	$(COMPOSE) up --build --detach

run:              ## one more ingest run against the running stack
	$(COMPOSE) run --rm worker run

logs:
	$(COMPOSE) logs --follow worker

test:             ## unit + integration tests inside the stack
	$(COMPOSE) --profile test run --rm --build tests

config:           ## print the worker's effective configuration (secrets redacted)
	$(COMPOSE) run --rm --no-deps worker config

down:
	$(COMPOSE) down

clean:            ## stop and delete volumes (database and outputs)
	$(COMPOSE) down --volumes --remove-orphans
