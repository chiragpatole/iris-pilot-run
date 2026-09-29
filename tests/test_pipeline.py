import hashlib
import json

from iris_run import freshness
from iris_run.cli import main
from iris_run.stages import LOCK_KEY, Context, promote


def run(db_url, out):
    return main(["run", "--db", db_url, "--out", str(out)])


def manifest(out):
    return json.loads((out / "manifest.latest.json").read_text())


def stage(m, name):
    return next(s for s in m["stages"] if s["name"] == name)


def stale_items(m):
    return {i["item"] for i in m["freshness"]["items"] if i["stale"]}


def dossier_hashes(out):
    return {
        str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((out / "dossiers").rglob("*.md"))
    }


# ---------------------------------------------------------------- happy path

def test_one_command_produces_both_verticals(db, test_db_url, tmp_path):
    code = run(test_db_url, tmp_path)
    m = manifest(tmp_path)

    assert code == 0
    assert m["status"] == "success"
    assert all(s["status"] == "success" for s in m["stages"])

    assert len(list((tmp_path / "dossiers" / "bess").glob("*.md"))) == 3
    assert len(list((tmp_path / "dossiers" / "peatland").glob("*.md"))) == 3
    assert m["freshness"]["available"] and stale_items(m) == set()

    p = stage(m, "promote")["details"]
    assert (p["accepted_rows"], p["inserted"], p["rejected"], p["not_accepted_rows_ignored"]) == (16, 8, 8, 1)


def test_dossiers_carry_the_uncertainty_wording(db, test_db_url, tmp_path):
    run(test_db_url, tmp_path)
    text = (tmp_path / "dossiers" / "peatland" / "DE_P-004.md").read_text()
    assert "not certified compensation" in text
    assert "No permit, reservation or construction readiness is represented." in text


# ---------------------------------------------------------------- idempotency

def test_rerun_changes_nothing(db, test_db_url, tmp_path):
    run(test_db_url, tmp_path)
    hashes_first = dossier_hashes(tmp_path)
    rows_first = db.execute("SELECT count(*) FROM core.parcel").fetchone()[0]

    assert run(test_db_url, tmp_path) == 0
    p = stage(manifest(tmp_path), "promote")["details"]

    assert (p["inserted"], p["updated"], p["unchanged"]) == (0, 0, 8)
    assert db.execute("SELECT count(*) FROM core.parcel").fetchone()[0] == rows_first
    assert dossier_hashes(tmp_path) == hashes_first
    assert stale_items(manifest(tmp_path)) == set()


# ------------------------------------------------------------ data contract

def test_bad_rows_are_rejected_with_a_reason_and_never_promoted(db, test_db_url, tmp_path):
    run(test_db_url, tmp_path)
    reasons = {(r["source_id"], r["reason"].split(":")[0]) for r in stage(manifest(tmp_path), "promote")["details"]["rejections"]}

    assert ("P-007", "missing country_code") in reasons
    assert ("P-008", "invalid geometry") in reasons
    assert ("P-009", "missing CRS (srid)") in reasons
    assert ("P-010", "missing source_date") in reasons
    assert ("P-012", "same (country_code, source_id) appears more than once in the batch") in reasons
    assert ("P-013", "unsupported CRS 3857") in reasons
    assert ("P-014", "unknown land_type forest") in reasons

    promoted = {r[0] for r in db.execute("SELECT site_id FROM core.parcel WHERE country_code = 'DE'")}
    assert promoted == {"P-001", "P-002", "P-003", "P-004", "P-005", "P-011"}
    assert db.execute("SELECT count(*) FROM core.parcel WHERE country_code IS NULL").fetchone()[0] == 0


def test_source_crs_is_transformed_and_kept(db, test_db_url, tmp_path):
    run(test_db_url, tmp_path)
    srid, source_srid, area = db.execute(
        "SELECT ST_SRID(geom), source_srid, area_m2 FROM core.parcel WHERE country_code = 'DE' AND site_id = 'P-011'"
    ).fetchone()

    # the fixture is a 200 m x 150 m rectangle in EPSG:25832
    assert (srid, source_srid) == (4326, 25832)
    assert abs(float(area) - 30000) < 60


# ------------------------------------------------------------ country scoping

def test_same_id_in_two_countries_stays_separate(db, test_db_url, tmp_path):
    run(test_db_url, tmp_path)
    countries = {r[0] for r in db.execute("SELECT country_code FROM core.parcel WHERE site_id = 'P-001'")}
    assert countries == {"DE", "AT"}


def test_substation_match_never_crosses_a_border(db, test_db_url, tmp_path):
    run(test_db_url, tmp_path)
    # an AT substation sits inside DE parcel P-003, but only the DE one may be used
    distance = db.execute(
        "SELECT distance_m FROM mart.bess_candidates WHERE country_code = 'DE' AND site_id = 'P-003'"
    ).fetchone()[0]
    assert distance > 300


# ------------------------------------------------------------ failure visibility

def test_broken_view_is_partial_and_the_stale_export_is_visible(db, test_db_url, tmp_path):
    run(test_db_url, tmp_path)
    old_bess_files = {k: v for k, v in dossier_hashes(tmp_path).items() if "bess" in k}

    # real data change, so promotion bumps the data timestamp
    db.execute("UPDATE staging.parcel_raw SET name = 'Wiese Nord (umbenannt)' WHERE country_code = 'DE' AND source_id = 'P-001'")
    # and a BESS view that fails as soon as it is refreshed
    db.execute("DROP MATERIALIZED VIEW mart.bess_candidates")
    db.execute("CREATE MATERIALIZED VIEW mart.bess_candidates AS SELECT 1 / 0 AS x WITH NO DATA")

    code = run(test_db_url, tmp_path)
    m = manifest(tmp_path)

    assert code == 2 and m["status"] == "partial"
    assert stage(m, "promote")["details"]["updated"] == 1
    assert stage(m, "refresh_bess")["status"] == "failed"
    assert "division by zero" in stage(m, "refresh_bess")["error"]["message"]
    assert stage(m, "export_bess")["status"] == "skipped"
    assert stage(m, "export_peatland")["status"] == "success"

    # the old BESS dossiers are still on disk, and the manifest says they are out of date
    assert {k: v for k, v in dossier_hashes(tmp_path).items() if "bess" in k} == old_bess_files
    assert stale_items(m) == {"view:bess", "export:bess"}


def test_failed_critical_stage_stops_the_run_and_is_reported_as_failed(db, test_db_url, tmp_path):
    db.execute("TRUNCATE staging.parcel_raw")

    code = run(test_db_url, tmp_path)
    m = manifest(tmp_path)

    assert code == 1 and m["status"] == "failed"
    assert stage(m, "preflight")["status"] == "failed"
    assert "nothing to promote" in stage(m, "preflight")["error"]["message"]
    assert all(s["status"] == "skipped" for s in m["stages"] if s["name"] != "preflight")
    assert not (tmp_path / "dossiers").exists()


def test_unreachable_database_still_leaves_a_manifest(tmp_path):
    code = run("postgresql://iris:iris@localhost:1/iris", tmp_path)
    m = manifest(tmp_path)

    assert code == 1 and m["status"] == "failed"
    assert stage(m, "preflight")["status"] == "failed"
    assert m["freshness"]["available"] is False


def test_two_runs_at_once_are_refused(db, test_db_url, tmp_path):
    from iris_run.db import connect

    other = connect(test_db_url)
    other.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        code = run(test_db_url, tmp_path)
    finally:
        other.close()

    assert code == 1
    assert "another run" in stage(manifest(tmp_path), "preflight")["error"]["message"]


# ---------------------------------------------------------------- staleness

def test_new_data_makes_views_and_exports_visibly_stale_until_the_next_run(db, test_db_url, tmp_path):
    run(test_db_url, tmp_path)
    assert stale_items(manifest(tmp_path)) == set()

    db.execute(
        """
        INSERT INTO staging.parcel_raw (country_code, source_id, name, land_type, srid, source_date, review_status, wkt)
        VALUES ('DE', 'P-099', 'Neues Moor', 'peatland', 4326, '2026-03-10', 'accepted',
                'POLYGON((10.2400 47.9600, 10.2420 47.9600, 10.2420 47.9620, 10.2400 47.9620, 10.2400 47.9600))')
        """
    )
    ctx = Context(test_db_url, tmp_path)
    try:
        promote(ctx)  # promotion only, nothing refreshed yet
    finally:
        ctx.close()

    assert {i["item"] for i in freshness.report(db) if i["stale"]} == {
        "view:bess", "view:peatland", "export:bess", "export:peatland",
    }

    run(test_db_url, tmp_path)
    assert stale_items(manifest(tmp_path)) == set()
