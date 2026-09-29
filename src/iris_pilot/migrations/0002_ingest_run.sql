-- One row per worker run. Identifiers are country-scoped: the primary key
-- is (country_code, run_id) and every child table joins on both columns.

CREATE TABLE iris.ingest_run (
    country_code   text        NOT NULL,
    run_id         bigint      GENERATED ALWAYS AS IDENTITY,
    region_code    text        NOT NULL,
    source_uri     text        NOT NULL,
    source_sha256  text,
    source_date    date,
    source_srid    integer,
    status         text        NOT NULL DEFAULT 'running',
    feature_count  integer,
    accepted_count integer,
    rejected_count integer,
    error          text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    CONSTRAINT ingest_run_pkey PRIMARY KEY (country_code, run_id),
    CONSTRAINT ingest_run_country_code_check CHECK (country_code ~ '^[A-Z]{2}$'),
    CONSTRAINT ingest_run_region_code_check CHECK (region_code ~ '^[A-Z0-9]{1,3}$'),
    CONSTRAINT ingest_run_status_check CHECK (status IN ('running', 'succeeded', 'failed')),
    CONSTRAINT ingest_run_finished_check CHECK ((status = 'running') = (finished_at IS NULL)),
    CONSTRAINT ingest_run_sha256_check CHECK (source_sha256 IS NULL OR source_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ingest_run_counts_check CHECK (
        status <> 'succeeded'
        OR (feature_count = accepted_count + rejected_count AND source_date IS NOT NULL AND source_srid IS NOT NULL)
    )
);

CREATE INDEX ingest_run_scope_idx ON iris.ingest_run (country_code, region_code, started_at DESC);

COMMENT ON TABLE iris.ingest_run IS 'Audit trail of worker runs per country/region scope.';
COMMENT ON COLUMN iris.ingest_run.source_uri IS 'Source endpoint with credentials and query values redacted.';
COMMENT ON COLUMN iris.ingest_run.source_date IS 'Reference date declared by the source (data contract), not the fetch time.';
COMMENT ON COLUMN iris.ingest_run.source_srid IS 'CRS the source coordinates were declared in, before transformation to EPSG:4326.';
