# The wording below is the project's own uncertainty text, copied as given.
# It goes into every dossier, unchanged.
DISCLAIMER = (
    "Preliminary prospecting material. Figures, eco-point estimates and site suitability are "
    "indicative and based on available source data and commercial screening assumptions. "
    "The 8 eco-points/m2 factor is the current commercial baseline, not certified compensation. "
    "Ownership, planning, grid capacity, environmental eligibility and transferability remain "
    "subject to project-specific verification. No permit, reservation or construction "
    "readiness is represented."
)


def _area(value):
    m2 = float(value)
    return f"{m2:,.0f} m2 ({m2 / 10000:.2f} ha)"


def render_bess(row):
    return f"""# BESS screening: {row['country_code']} / {row['site_id']}

{row['name'] or '(unnamed)'}, {row['land_type']}

| | |
|---|---|
| Country / site id | {row['country_code']} / {row['site_id']} |
| Area | {_area(row['area_m2'])} |
| Nearest substation | {row['nearest_substation_id']}, {row['substation_name']} ({row['voltage_kv']} kV) |
| Distance to substation | {float(row['distance_m']):,.0f} m |
| Source date | {row['source_date']} |

Area and distance are measured on the WGS84 ellipsoid, in metres. Only substations
with the same country code as the parcel are considered.

## Limits

{DISCLAIMER}
"""


def render_peatland(row):
    return f"""# Peatland screening: {row['country_code']} / {row['site_id']}

{row['name'] or '(unnamed)'}

| | |
|---|---|
| Country / site id | {row['country_code']} / {row['site_id']} |
| Area | {_area(row['area_m2'])} |
| Eco-point factor | {row['eco_point_factor']} eco-points/m2 (commercial baseline) |
| Indicative eco-points | {row['eco_points_estimate']:,} |
| Source date | {row['source_date']} |

The eco-point figure is area multiplied by the baseline factor. It is an estimate for
screening only.

## Limits

{DISCLAIMER}
"""


RENDERERS = {"bess": render_bess, "peatland": render_peatland}
