# Project IRIS: one-command pilot run

My solution for IRIS-CAND-22. One command promotes the accepted parcels, refreshes the BESS and peatland views, exports a few dossiers per vertical, and writes a manifest that says what happened. If a stage breaks, the manifest and the exit code say so, and the stage after it does not pretend everything is fine.

## Running it

You need Docker, Python 3.12+ and make (or just copy the commands out of the Makefile).

```
docker compose up -d --wait        # PostGIS 16 on port 5433
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

make setup                         # migrations + sample data
make run                           # the one command
make test
```

On an Apple Silicon Mac the database image runs under emulation (the `platform` line in the compose file), so the first start takes a little longer. It works fine.

If you already have a PostgreSQL 16 / PostGIS 3.4 database, set `DATABASE_URL` and skip Docker. The database user has to be allowed to create the `postgis` extension.

The tests create their own `iris_test` database on the same server and wipe it between tests, so they never touch the database you run the pipeline against.

`make run` prints a short summary and writes:

- `out/manifest.latest.json` and one file per run in `out/runs/`
- `out/dossiers/bess/` and `out/dossiers/peatland/`

Exit codes: 0 success, 1 failed, 2 partial. `make status` shows what is current and what is stale without running anything, and exits 1 if anything is stale.

The `examples/` folder has the manifest from a normal run and one from a run where I broke the BESS view on purpose.

## What a run does

1. **preflight** (critical). PostgreSQL 16+, PostGIS 3.4+, migrations applied, both views exist, staging has accepted rows, output folder is writable, and no other run is using the database (advisory lock).
2. **promote** (critical). Accepted rows in `staging.parcel_raw` go into `core.parcel` if they meet the data contract.
3. **refresh, then export, once per vertical.** The BESS chain and the peatland chain do not depend on each other.

What the status means:

- A critical stage fails: the run stops, everything after it is marked `skipped`, status is `failed`.
- A vertical fails (say the BESS refresh): its export is skipped, the other vertical still runs, status is `partial`.
- `success` only if every stage succeeded.

The manifest is written in every case, including when the database is down. In that case the freshness section says it is unavailable instead of guessing.

## Rules I applied to the data

- The geometry column is called `geom`, stored as MultiPolygon in EPSG:4326. The original SRID is kept in `source_srid`.
- A parcel needs a two-letter upper case `country_code`, a `source_id`, a geometry, a CRS from a short list (4326, 25832, 25833, 3035), a source date and a known land type. If any of that is missing the row is rejected, and the reason is in the manifest. Nothing is filled in with a default.
- Invalid geometries are rejected, not repaired. `ST_MakeValid` would give me a shape, but it would be my guess about what the source meant.
- If the same `(country_code, source_id)` shows up twice in one accepted batch, both rows are rejected. I cannot tell which one is right.
- Keys and joins are country scoped. The primary key is `(country_code, site_id)` and a parcel is only matched with substations from its own country. The fixtures include an AT substation sitting inside a DE parcel to check exactly that.
- Areas and distances use `geography`, so they come out in metres without picking a UTM zone.
- Every dossier carries the project's uncertainty wording unchanged. The eco-point factor (8 per m2) is a visible column in the view and in the dossier, labelled as a baseline.

## Stale state and reruns

Postgres does not keep track of when a materialized view was last refreshed, so `ops.freshness` does. There are five items: `core_data`, a view per vertical, and an export per vertical.

- A view is stale if the promoted data changed after the view was refreshed.
- An export is stale if its view is stale, or if the view was refreshed after the export was written.
- The refresh and its timestamp are in one transaction, so a failed refresh cannot mark the view as fresh. Exports stamp their timestamp last, so a crash halfway leaves them stale.

When a vertical fails, the old dossiers stay on disk, and the manifest lists them as stale. I did not want a failed run to delete the last good output.

Running again is safe:

- Promotion is an upsert that only touches a row if something in it actually differs. A second run reports `0 new, 0 changed, 8 unchanged`, and `core_data` is not bumped.
- Views are refreshed in full every run.
- Dossiers contain no timestamps, are written to a temp file and renamed, and come out byte for byte the same when the data has not changed. Dossiers from an earlier run that are no longer in the sample are removed.
- An advisory lock stops two runs from overlapping.

## Simplifications, and what I would do with real data

| What I did | What I would do in production |
|---|---|
| Any promoted change marks every view stale, even if only peatland data changed | Track which tables each view reads and only mark the affected ones |
| `REFRESH MATERIALIZED VIEW` without `CONCURRENTLY` | Use `CONCURRENTLY` so readers are not blocked. The unique indexes it needs are already there |
| Dossiers are Markdown | Render the two-page PDF. I kept Markdown because it is easy to diff and stays deterministic |
| Promotion never deletes | Decide with the data owners how a parcel gets retired |
| Empty staging, or a view with no rows, counts as a failure | Make it configurable, an empty batch can be legitimate in a live setup |
| Substations are reference data loaded directly, without the checks parcels get | Give them the same staging and contract path |
| BESS thresholds (at least 1 ha, substation within 2 km) are literals in `002_views.sql` | Move them into a small rules table with a version, and bump freshness when they change |
| Everything is a single-region toy set | Add adapters per country and source, each writing to staging in the same shape |
| Fixtures are made up. The coordinates sit roughly in the Allgäu, and DE/AT are only labels | Real extracts, with a source and licence recorded per dataset |

## Tests

`make test` runs 19 tests.

Runner tests (no database, fake stages):
- a failed critical stage is never reported as success, later stages are skipped
- a critical failure after a successful optional stage is still `failed`, not `partial`
- one vertical failing gives `partial` and the other vertical still runs
- a failing prerequisite check stops the stage before it runs
- a stage that needs something later in the list is refused

Pipeline tests (real PostGIS):
- one command produces both verticals, and the row counts match the fixtures
- every dossier includes the project's uncertainty wording
- a rerun changes nothing, and the dossiers are byte identical
- every kind of bad row is rejected with its reason and never promoted
- a parcel in EPSG:25832 is transformed and its area is right
- the same id in two countries stays separate, and a substation match never crosses a border
- a broken view gives `partial`, the export is skipped, and the stale state shows up in the manifest
- an empty staging table fails preflight and everything else is skipped
- an unreachable database still leaves a manifest behind
- two runs at once are refused
- new data makes views and exports show as stale until the next run

## Layout

```
iris_run/        the package
  cli.py         migrate, load-fixtures, run, status
  runner.py      stage runner and the status rules
  stages.py      preflight, promote, refresh, export
  freshness.py   what is current and what is stale
  manifest.py    manifest file and console summary
  dossier.py     dossier text
  fixtures.py    loads the sample CSVs
  migrate.py     applies migrations/*.sql
  db.py          connection helpers
migrations/      001 schema, 002 views
fixtures/        parcels.csv, substations.csv
tests/
examples/        two real manifests
```
