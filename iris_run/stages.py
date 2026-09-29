import hashlib
import os
import re
from pathlib import Path

import psycopg

from . import dossier, freshness
from .db import connect, query
from .migrate import applied_migrations, migration_files
from .runner import PrerequisiteError, Stage

# any number works, it only has to be the same for every run
LOCK_KEY = 60610061

MIN_POSTGRES = 160000
MIN_POSTGIS = (3, 4)

VIEWS = {
    "bess": "mart.bess_candidates",
    "peatland": "mart.peatland_candidates",
}

ORDER_BY = {
    "bess": "distance_m, area_m2 DESC, country_code, site_id",
    "peatland": "area_m2 DESC, country_code, site_id",
}


class Context:
    """Holds settings and one database connection for the whole run."""

    def __init__(self, db_url, out_dir, sample_size=3):
        self.db_url = db_url
        self.out_dir = Path(out_dir)
        self.sample_size = sample_size
        self._conn = None

    @property
    def conn(self):
        # connects the first time it is used, so a dead database shows up
        # inside the preflight stage and not as a crash before the run starts
        if self._conn is None:
            self._conn = connect(self.db_url)
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()  # also drops the advisory lock
            self._conn = None


# ---------------------------------------------------------------- preflight

def preflight(ctx):
    conn = ctx.conn

    pg_num = int(conn.execute("SHOW server_version_num").fetchone()[0])
    if pg_num < MIN_POSTGRES:
        raise PrerequisiteError(f"PostgreSQL 16 or newer is required, this server reports {pg_num}")

    try:
        postgis = conn.execute("SELECT postgis_lib_version()").fetchone()[0]
    except psycopg.errors.UndefinedFunction:
        raise PrerequisiteError("PostGIS is not installed in this database") from None
    if tuple(int(x) for x in postgis.split(".")[:2]) < MIN_POSTGIS:
        raise PrerequisiteError(f"PostGIS 3.4 or newer is required, found {postgis}")

    done = applied_migrations(conn)
    pending = [f.name for f in migration_files() if f.name not in done]
    if pending:
        raise PrerequisiteError("migrations not applied: " + ", ".join(pending) + " (run: python -m iris_run migrate)")

    have = {r[0] for r in conn.execute("SELECT schemaname || '.' || matviewname FROM pg_matviews")}
    missing = [v for v in VIEWS.values() if v not in have]
    if missing:
        raise PrerequisiteError("materialized views missing: " + ", ".join(missing))

    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]:
        raise PrerequisiteError("another run is already using this database")

    waiting = conn.execute(
        "SELECT count(*) FROM staging.parcel_raw WHERE review_status = 'accepted'"
    ).fetchone()[0]
    if waiting == 0:
        raise PrerequisiteError("staging has no accepted rows, nothing to promote (did you run load-fixtures?)")

    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(ctx.out_dir, os.W_OK):
        raise PrerequisiteError(f"output folder {ctx.out_dir} is not writable")

    return {"postgres_version_num": pg_num, "postgis": postgis, "accepted_rows_waiting": waiting}


# ------------------------------------------------------------------ promote

# Step 1: look at every accepted staging row and decide if it meets the data
# contract. Nothing is repaired or guessed, a row either passes or gets a reason.
CHECK_SQL = """
CREATE TEMP TABLE _checked ON COMMIT DROP AS
WITH parsed AS (
    SELECT r.*,
           CASE WHEN r.wkt IS NOT NULL AND r.srid IS NOT NULL
                THEN core.safe_geom(r.wkt, r.srid) END AS g,
           count(*) OVER (PARTITION BY r.country_code, r.source_id) AS same_key
    FROM staging.parcel_raw r
    WHERE r.review_status = 'accepted'
)
SELECT p.*,
       CASE
           WHEN p.country_code IS NULL OR btrim(p.country_code) = '' THEN 'missing country_code'
           WHEN p.country_code !~ '^[A-Z]{2}$' THEN 'country_code is not a two-letter upper case code'
           WHEN p.source_id IS NULL OR btrim(p.source_id) = '' THEN 'missing source_id'
           WHEN p.wkt IS NULL THEN 'missing geometry'
           WHEN p.srid IS NULL THEN 'missing CRS (srid)'
           WHEN p.srid NOT IN (4326, 25832, 25833, 3035) THEN 'unsupported CRS ' || p.srid
           WHEN p.g IS NULL THEN 'geometry could not be parsed'
           WHEN GeometryType(p.g) NOT IN ('POLYGON', 'MULTIPOLYGON') THEN 'geometry is not a polygon'
           WHEN ST_IsEmpty(p.g) THEN 'geometry is empty'
           WHEN NOT ST_IsValid(p.g) THEN 'invalid geometry: ' || ST_IsValidReason(p.g)
           WHEN p.source_date IS NULL THEN 'missing source_date'
           WHEN p.land_type IS NULL OR p.land_type NOT IN ('open_land', 'industrial', 'peatland')
                THEN 'unknown land_type ' || coalesce(p.land_type, 'NULL')
           WHEN p.same_key > 1 THEN 'same (country_code, source_id) appears more than once in the batch'
       END AS reject_reason
FROM parsed p
"""

# Step 2: upsert the good rows. A row only counts as changed if something
# actually differs, which is what makes running this twice a no-op.
UPSERT_SQL = """
WITH up AS (
    INSERT INTO core.parcel AS t
        (country_code, site_id, name, land_type, geom, area_m2, source_srid, source_date)
    SELECT c.country_code,
           c.source_id,
           c.name,
           c.land_type,
           ST_Multi(ST_Transform(c.g, 4326)),
           round(ST_Area(ST_Transform(c.g, 4326)::geography)::numeric, 2),
           c.srid,
           c.source_date
    FROM _checked c
    WHERE c.reject_reason IS NULL
    ON CONFLICT (country_code, site_id) DO UPDATE
        SET name = EXCLUDED.name,
            land_type = EXCLUDED.land_type,
            geom = EXCLUDED.geom,
            area_m2 = EXCLUDED.area_m2,
            source_srid = EXCLUDED.source_srid,
            source_date = EXCLUDED.source_date,
            updated_at = clock_timestamp()
        WHERE (t.name, t.land_type, t.area_m2, t.source_srid, t.source_date, ST_AsBinary(t.geom))
              IS DISTINCT FROM
              (EXCLUDED.name, EXCLUDED.land_type, EXCLUDED.area_m2, EXCLUDED.source_srid,
               EXCLUDED.source_date, ST_AsBinary(EXCLUDED.geom))
    RETURNING (xmax = 0) AS was_insert
)
SELECT count(*) FILTER (WHERE was_insert),
       count(*) FILTER (WHERE NOT was_insert)
FROM up
"""


def promote(ctx):
    conn = ctx.conn
    with conn.transaction():
        conn.execute(CHECK_SQL)
        accepted = conn.execute("SELECT count(*) FROM _checked").fetchone()[0]
        rejections = query(
            conn,
            "SELECT country_code, source_id, reject_reason AS reason "
            "FROM _checked WHERE reject_reason IS NOT NULL ORDER BY id",
        )
        valid = accepted - len(rejections)
        if accepted and not valid:
            # every row bad usually means the feed itself is broken
            raise RuntimeError("every accepted row failed the data contract, nothing was promoted")

        inserted, updated = conn.execute(UPSERT_SQL).fetchone()
        if inserted or updated:
            freshness.touch(conn, "core_data")

    not_accepted = conn.execute(
        "SELECT count(*) FROM staging.parcel_raw WHERE review_status <> 'accepted'"
    ).fetchone()[0]

    return {
        "accepted_rows": accepted,
        "inserted": inserted,
        "updated": updated,
        "unchanged": valid - inserted - updated,
        "rejected": len(rejections),
        "not_accepted_rows_ignored": not_accepted,
        "rejections": rejections[:50],
    }


# ------------------------------------------------------------------ refresh

def refresh_stage(vertical):
    view = VIEWS[vertical]

    def refresh(ctx):
        conn = ctx.conn
        # refresh and freshness stamp in one transaction: if the refresh
        # fails, the view is not marked as fresh
        with conn.transaction():
            conn.execute(f"REFRESH MATERIALIZED VIEW {view}")
            rows = conn.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
            freshness.touch(conn, f"view:{vertical}")
        return {"view": view, "rows": rows}

    return Stage(f"refresh_{vertical}", refresh, needs=("promote",))


# ------------------------------------------------------------------- export

def _write_atomic(path, text):
    # write next to the target and rename, so nobody ever reads a half-written file
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def export_stage(vertical):
    view = VIEWS[vertical]
    render = dossier.RENDERERS[vertical]

    def check(ctx):
        state = next(e for e in freshness.report(ctx.conn) if e["item"] == f"view:{vertical}")
        if state["stale"]:
            raise PrerequisiteError(f"{view} is stale, not exporting from it ({state['reason']})")

    def export(ctx):
        rows = query(
            ctx.conn,
            f"SELECT * FROM {view} ORDER BY {ORDER_BY[vertical]} LIMIT %s",
            (ctx.sample_size,),
        )
        if not rows:
            raise RuntimeError(f"{view} has no rows, there is nothing to export")

        folder = ctx.out_dir / "dossiers" / vertical
        folder.mkdir(parents=True, exist_ok=True)

        files = []
        for row in rows:
            name = re.sub(r"[^A-Za-z0-9_-]", "_", f"{row['country_code']}_{row['site_id']}") + ".md"
            text = render(row)
            _write_atomic(folder / name, text)
            files.append({
                "path": str((folder / name).relative_to(ctx.out_dir)),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })

        # drop dossiers left over from an earlier run that are no longer in the sample
        keep = {Path(f["path"]).name for f in files}
        for old in folder.glob("*.md"):
            if old.name not in keep:
                old.unlink()

        # stamped last on purpose: a crash halfway leaves the export marked stale
        freshness.touch(ctx.conn, f"export:{vertical}")
        return {"view": view, "dossiers": len(files), "files": files}

    return Stage(f"export_{vertical}", export, needs=(f"refresh_{vertical}",), check=check)


# ------------------------------------------------------------------- wiring

def build_stages():
    stages = [
        Stage("preflight", preflight, critical=True),
        Stage("promote", promote, critical=True, needs=("preflight",)),
    ]
    for vertical in freshness.VERTICALS:
        stages.append(refresh_stage(vertical))
        stages.append(export_stage(vertical))
    return stages
