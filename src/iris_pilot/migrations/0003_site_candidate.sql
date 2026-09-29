-- Candidate sites promoted from a validated source, plus the features that
-- were rejected (with the reason) so nothing disappears silently.

CREATE TABLE iris.site_candidate (
    country_code          text             NOT NULL,
    region_code           text             NOT NULL,
    site_id               text             NOT NULL,
    name                  text,
    source_date           date             NOT NULL,
    positional_accuracy_m double precision,
    area_m2               double precision NOT NULL,
    geom                  geometry(MultiPolygon, 4326) NOT NULL,
    ingest_run_id         bigint           NOT NULL,
    created_at            timestamptz      NOT NULL DEFAULT now(),
    updated_at            timestamptz      NOT NULL DEFAULT now(),
    CONSTRAINT site_candidate_pkey PRIMARY KEY (country_code, region_code, site_id),
    CONSTRAINT site_candidate_run_fkey FOREIGN KEY (country_code, ingest_run_id)
        REFERENCES iris.ingest_run (country_code, run_id),
    CONSTRAINT site_candidate_country_code_check CHECK (country_code ~ '^[A-Z]{2}$'),
    CONSTRAINT site_candidate_region_code_check CHECK (region_code ~ '^[A-Z0-9]{1,3}$'),
    CONSTRAINT site_candidate_site_id_check CHECK (btrim(site_id) <> ''),
    CONSTRAINT site_candidate_geom_check CHECK (ST_IsValid(geom) AND NOT ST_IsEmpty(geom)),
    CONSTRAINT site_candidate_area_check CHECK (area_m2 > 0),
    CONSTRAINT site_candidate_accuracy_check CHECK (positional_accuracy_m IS NULL OR positional_accuracy_m >= 0)
);

CREATE INDEX site_candidate_geom_idx ON iris.site_candidate USING gist (geom);
CREATE INDEX site_candidate_run_idx ON iris.site_candidate (country_code, ingest_run_id);

COMMENT ON TABLE iris.site_candidate IS 'Current validated candidate sites per country/region scope.';
COMMENT ON COLUMN iris.site_candidate.site_id IS 'Identifier from the source; unique only within its country/region scope.';
COMMENT ON COLUMN iris.site_candidate.geom IS 'Canonical geometry, EPSG:4326, 2D, validated (never repaired silently).';
COMMENT ON COLUMN iris.site_candidate.area_m2 IS 'Geodesic area in square metres on the WGS84 spheroid.';
COMMENT ON COLUMN iris.site_candidate.positional_accuracy_m IS 'Source-declared positional uncertainty in metres; NULL means unknown, not zero.';

CREATE TABLE iris.ingest_rejection (
    country_code  text    NOT NULL,
    ingest_run_id bigint  NOT NULL,
    feature_index integer NOT NULL,
    site_id       text,
    reason        text    NOT NULL,
    detail        text    NOT NULL,
    CONSTRAINT ingest_rejection_pkey PRIMARY KEY (country_code, ingest_run_id, feature_index),
    CONSTRAINT ingest_rejection_run_fkey FOREIGN KEY (country_code, ingest_run_id)
        REFERENCES iris.ingest_run (country_code, run_id) ON DELETE CASCADE,
    CONSTRAINT ingest_rejection_country_code_check CHECK (country_code ~ '^[A-Z]{2}$'),
    CONSTRAINT ingest_rejection_index_check CHECK (feature_index >= 0)
);

COMMENT ON TABLE iris.ingest_rejection IS 'Source features that failed the data contract, per run.';
COMMENT ON COLUMN iris.ingest_rejection.feature_index IS 'Zero-based position of the feature in the source collection.';
