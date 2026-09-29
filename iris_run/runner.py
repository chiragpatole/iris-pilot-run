import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


class PrerequisiteError(Exception):
    """A stage could not start because something it relies on is missing."""


@dataclass
class Stage:
    name: str
    run: Callable
    critical: bool = False      # if this fails, stop the whole run
    needs: tuple = ()           # stages that must have succeeded first
    check: Callable | None = None  # runs right before `run`, raises PrerequisiteError


@dataclass
class StageResult:
    name: str
    status: str                 # success | failed | skipped
    critical: bool
    started_at: str | None = None
    finished_at: str | None = None
    seconds: float = 0.0
    details: dict = field(default_factory=dict)
    error: dict | None = None
    skipped_because: str | None = None


def utcnow():
    return datetime.now(timezone.utc)


def stamp(dt):
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def one_line(text):
    # database errors often span several lines, the summary table wants one
    return " ".join(str(text).split())


def _describe(exc):
    last = traceback.extract_tb(exc.__traceback__)[-1]
    return {
        "type": type(exc).__name__,
        "message": one_line(exc)[:500],
        "where": f"{last.filename.split('/')[-1]}:{last.lineno} in {last.name}",
    }


def _run_one(stage, ctx):
    started = utcnow()
    t0 = time.monotonic()
    result = StageResult(stage.name, "success", stage.critical, started_at=stamp(started))
    try:
        if stage.check:
            stage.check(ctx)
        result.details = stage.run(ctx) or {}
    except Exception as exc:
        result.status = "failed"
        result.error = _describe(exc)
    result.seconds = round(time.monotonic() - t0, 3)
    result.finished_at = stamp(utcnow())
    return result


def run_stages(stages, ctx):
    seen = set()
    for stage in stages:
        unknown = [n for n in stage.needs if n not in seen]
        if unknown:
            raise ValueError(f"stage '{stage.name}' needs {unknown}, which do not come before it")
        seen.add(stage.name)

    results = {}
    stopped_by = None

    for stage in stages:
        if stopped_by:
            results[stage.name] = StageResult(
                stage.name, "skipped", stage.critical,
                skipped_because=f"run stopped, critical stage '{stopped_by}' failed",
            )
            continue

        not_ok = [n for n in stage.needs if results[n].status != "success"]
        if not_ok:
            results[stage.name] = StageResult(
                stage.name, "skipped", stage.critical,
                skipped_because="needs " + ", ".join(not_ok) + " which did not succeed",
            )
            continue

        result = _run_one(stage, ctx)
        results[stage.name] = result
        if result.status == "failed" and stage.critical:
            stopped_by = stage.name

    return list(results.values())


def overall_status(results):
    """
    success: everything worked
    partial: only non-critical stages failed and at least one of them still succeeded
    failed:  anything else. A failed critical stage always ends up here.
    """
    if all(r.status == "success" for r in results):
        return "success"
    if any(r.status == "failed" and r.critical for r in results):
        return "failed"
    if any(r.status == "success" and not r.critical for r in results):
        return "partial"
    return "failed"


def exit_code(status):
    return {"success": 0, "failed": 1, "partial": 2}[status]
