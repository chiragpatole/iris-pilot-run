import json
import os
from dataclasses import asdict

from .runner import overall_status, stamp


def build_manifest(run_id, started, finished, results, fresh):
    status = overall_status(results)
    return {
        "run_id": run_id,
        "status": status,
        "started_at": stamp(started),
        "finished_at": stamp(finished),
        "seconds": round((finished - started).total_seconds(), 3),
        "stages": [asdict(r) for r in results],
        "outputs": [f for r in results for f in r.details.get("files", [])],
        "freshness": fresh,
    }


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_manifest(out_dir, manifest):
    """One file per run, plus manifest.latest.json for whoever wants the newest."""
    run_file = out_dir / "runs" / f"{manifest['run_id']}.json"
    _write_json(run_file, manifest)
    _write_json(out_dir / "manifest.latest.json", manifest)
    return run_file


def _summary(result):
    d = result.details
    if result.status == "skipped":
        return result.skipped_because
    if result.status == "failed":
        return f"{result.error['type']}: {result.error['message']}"
    if result.name == "preflight":
        return f"postgres ok, postgis {d['postgis']}, {d['accepted_rows_waiting']} accepted rows waiting"
    if result.name == "promote":
        return (f"{d['inserted']} new, {d['updated']} changed, {d['unchanged']} unchanged, "
                f"{d['rejected']} rejected")
    if result.name.startswith("refresh_"):
        return f"{d['rows']} rows in {d['view']}"
    if result.name.startswith("export_"):
        return f"{d['dossiers']} dossiers"
    return ""


def print_summary(manifest, results, run_file):
    print(f"run {manifest['run_id']}  status: {manifest['status'].upper()}  ({manifest['seconds']}s)")
    print()
    for r in results:
        print(f"  {r.name:<18} {r.status:<8} {_summary(r)}")

    print()
    fresh = manifest["freshness"]
    if not fresh["available"]:
        print(f"freshness: unavailable ({fresh['error']})")
    else:
        stale = [i for i in fresh["items"] if i["stale"]]
        if not stale:
            print("freshness: everything is current")
        else:
            print("freshness: STALE")
            for i in stale:
                print(f"  {i['item']:<16} {i['reason']}")

    print()
    print(f"manifest: {run_file}")
