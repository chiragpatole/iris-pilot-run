-- 002: the two vertical views
-- Areas and distances use geography so they come out in real metres
-- without having to pick a UTM zone for every country.


-- BESS (battery storage) candidates:
-- open or industrial land, at least 1 ha, with a substation from the SAME
-- country within 2 km. Only the closest substation is kept.
-- Thresholds are literals on purpose so they are easy to find and change.
CREATE MATERIALIZED VIEW mart.bess_candidates AS
SELECT p.country_code,
       p.site_id,
       p.name,
       p.land_type,
       p.area_m2,
       p.source_date,
       n.substation_id AS nearest_substation_id,
       n.substation_name,
       n.voltage_kv,
       round(n.distance_m::numeric, 1) AS distance_m
FROM core.parcel p
CROSS JOIN LATERAL (
    SELECT s.substation_id,
           s.name AS substation_name,
           s.voltage_kv,
           ST_Distance(p.geom::geography, s.geom::geography) AS distance_m
    FROM core.substation s
    WHERE s.country_code = p.country_code
      AND ST_DWithin(p.geom::geography, s.geom::geography, 2000)
    ORDER BY distance_m, s.substation_id
    LIMIT 1
) n
WHERE p.land_type IN ('open_land', 'industrial')
  AND p.area_m2 >= 10000;

CREATE UNIQUE INDEX bess_candidates_key ON mart.bess_candidates (country_code, site_id);


-- Peatland candidates: every peatland parcel, with the indicative eco-point
-- estimate. 8 eco-points per m2 is the current commercial baseline, not a
-- certified figure, which is why the factor is a visible column.
CREATE MATERIALIZED VIEW mart.peatland_candidates AS
SELECT p.country_code,
       p.site_id,
       p.name,
       p.area_m2,
       8 AS eco_point_factor,
       round(p.area_m2 * 8)::bigint AS eco_points_estimate,
       p.source_date
FROM core.parcel p
WHERE p.land_type = 'peatland';

CREATE UNIQUE INDEX peatland_candidates_key ON mart.peatland_candidates (country_code, site_id);
