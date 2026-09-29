import argparse
import json
import uuid
from pathlib import Path

import psycopg

from . import freshness
from .db import connect, default_url
from .fixtures import FIXTURES_DIR, load_fixtures
from .manifest import build_manifest, print_summary, write_manifest
from .migrate import migrate
from .runner import exit_code, one_line, run_stages, utcnow
from .stages import Context, build_stages


def _freshness_or_error(ctx):
    # if the database is gone we still want a manifest, so this never raises
    try:
        return {"available": True, "items": freshness.report(ctx.conn)}
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {one_line(exc)[:200]}"}


def cmd_migrate(args):
    with connect(args.db) as conn:
        applied = migrate(conn)
    print("applied: " + ", ".join(applied) if applied else "nothing to apply, already up to date")
    return 0


def cmd_load_fixtures(args):
    with connect(args.db) as conn:
        info = load_fixtures(conn, args.fixtures)
    print(f"loaded {info['parcel_rows']} parcel rows into staging, "
          f"{info['substation_rows']} substations ({info['substations_changed']} new or changed)")
    return 0


def cmd_run(args):
    ctx = Context(args.db, args.out, args.sample)
    run_id = utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    started = utcnow()
    try:
        results = run_stages(build_stages(), ctx)
        fresh = _freshness_or_error(ctx)
    finally:
        ctx.close()

    manifest = build_manifest(run_id, started, utcnow(), results, fresh)
    run_file = write_manifest(ctx.out_dir, manifest)
    print_summary(manifest, results, run_file)
    return exit_code(manifest["status"])


def cmd_status(args):
    with connect(args.db) as conn:
        items = freshness.report(conn)
    for i in items:
        state = "STALE  " if i["stale"] else "current"
        note = f"  {i['reason']}" if i["reason"] else ""
        print(f"{i['item']:<16} {state}  last built: {i['updated_at'] or 'never'}{note}")

    latest = Path(args.out) / "manifest.latest.json"
    if latest.exists():
        m = json.loads(latest.read_text())
        print()
        print(f"last run: {m['run_id']}  status: {m['status']}  finished: {m['finished_at']}")
    return 1 if any(i["stale"] for i in items) else 0


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=default_url(), help="database url (default: $DATABASE_URL or the docker compose one)")
    common.add_argument("--out", default="out", help="folder for the manifest and dossiers")

    parser = argparse.ArgumentParser(prog="iris_run", description="Project IRIS pilot run")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", parents=[common], help="apply the SQL migrations")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("load-fixtures", parents=[common], help="fill staging with the sample data")
    p.add_argument("--fixtures", default=FIXTURES_DIR, help="folder with parcels.csv and substations.csv")
    p.set_defaults(func=cmd_load_fixtures)

    p = sub.add_parser("run", parents=[common], help="promote, refresh views, export dossiers")
    p.add_argument("--sample", type=int, default=3, help="dossiers per vertical (default 3)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", parents=[common], help="show what is current and what is stale")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except psycopg.OperationalError as exc:
        print(f"could not talk to the database: {one_line(exc)}")
        return 1
