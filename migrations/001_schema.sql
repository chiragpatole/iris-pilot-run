-- 001: base schema
-- staging = raw landing zone, core = promoted data, mart = materialized views,
-- ops = bookkeeping for the pipeline itself

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS ops;


-- Raw parcels as they arrive. Almost everything is nullable on purpose:
-- this is where bad rows are allowed to exist so promotion can reject them
-- with a reason instead of the load itself blowing up.
CREATE TABLE staging.parcel_raw (
    id            bigserial PRIMARY KEY,
    country_code  text,
    source_id     text,
    name          text,
    land_type     text,
    srid          integer,
    source_date   date,
    review_status text NOT NULL DEFAULT 'pending'
                  CHECK (review_status IN ('pending', 'accepted', 'rejected')),
    wkt           text,
    loaded_at     timestamptz NOT NULL DEFAULT now()
);


-- Grid substations. Reference data, loaded directly.
CREATE TABLE core.substation (
    country_code  text NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
    substation_id text NOT NULL,
    name          text NOT NULL,
    voltage_kv    integer,
    source_date   date NOT NULL,
    geom          geometry(Point, 4326) NOT NULL,
    PRIMARY KEY (country_code, substation_id)
);

CREATE INDEX substation_geom_idx ON core.substation USING gist (geom);
-- distances are measured on geography, so index that expression too
CREATE INDEX substation_geog_idx ON core.substation USING gist ((geom::geography));


-- Promoted parcels. The data contract lives in these constraints:
-- country code, land type, CRS and source date must all be present.
-- ids are only unique inside a country, hence the composite key.
CREATE TABLE core.parcel (
    country_code text NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
    site_id      text NOT NULL,
    name         text,
    land_type    text NOT NULL CHECK (land_type IN ('open_land', 'industrial', 'peatland')),
    geom         geometry(MultiPolygon, 4326) NOT NULL,
    area_m2      numeric(14, 2) NOT NULL CHECK (area_m2 > 0),
    source_srid  integer NOT NULL,
    source_date  date NOT NULL,
    promoted_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (country_code, site_id)
);

CREATE INDEX parcel_geom_idx ON core.parcel USING gist (geom);
CREATE INDEX parcel_geog_idx ON core.parcel USING gist ((geom::geography));


-- When was each thing last built? Postgres does not remember when a
-- materialized view was refreshed, so the pipeline writes it down here.
-- items: core_data, view:<vertical>, export:<vertical>
CREATE TABLE ops.freshness (
    item       text PRIMARY KEY,
    updated_at timestamptz NOT NULL
);


-- Parse WKT without letting one broken row kill the whole promotion query.
-- Returns NULL if the text is not usable geometry.
CREATE FUNCTION core.safe_geom(wkt text, srid integer) RETURNS geometry
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN ST_SetSRID(ST_GeomFromText(wkt), srid);
EXCEPTION WHEN others THEN
    RETURN NULL;
END;
$$;
