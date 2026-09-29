-- Platform prerequisites. Fails loudly on an unsupported server instead of
-- letting later migrations break in less obvious ways.

CREATE EXTENSION IF NOT EXISTS postgis;

DO $$
DECLARE
    server_version integer := current_setting('server_version_num')::integer;
    postgis_version integer[] :=
        string_to_array(substring(postgis_lib_version() FROM '^[0-9]+\.[0-9]+'), '.')::integer[];
BEGIN
    IF server_version < 160000 THEN
        RAISE EXCEPTION USING MESSAGE =
            'PostgreSQL 16 or newer is required, found ' || current_setting('server_version');
    END IF;
    IF postgis_version < ARRAY[3, 4] THEN
        RAISE EXCEPTION USING MESSAGE =
            'PostGIS 3.4 or newer is required, found ' || postgis_lib_version();
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS iris;

COMMENT ON SCHEMA iris IS 'IRIS pilot business data. Every table is scoped by country_code.';
