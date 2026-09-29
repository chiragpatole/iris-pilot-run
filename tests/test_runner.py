"""
These tests use fake stages, so they need no database.
They cover the rules that matter most: a failed critical stage must never
end up looking like a success.
"""
from iris_run.runner import PrerequisiteError, Stage, exit_code, overall_status, run_stages


def ok(ctx):
    return {"n": 1}


def boom(ctx):
    raise RuntimeError("it broke")


def by_name(results):
    return {r.name: r for r in results}


def test_everything_ok_is_success():
    results = run_stages([Stage("a", ok, critical=True), Stage("b", ok, needs=("a",))], ctx=None)
    assert overall_status(results) == "success"
    assert exit_code("success") == 0


def test_failed_critical_stage_is_never_success():
    stages = [
        Stage("a", ok, critical=True),
        Stage("b", boom, critical=True, needs=("a",)),
        Stage("c", ok, needs=("b",)),
        Stage("d", ok),  # independent, but the run should still stop
    ]
    r = by_name(run_stages(stages, ctx=None))

    assert r["a"].status == "success"
    assert r["b"].status == "failed"
    assert r["b"].error["type"] == "RuntimeError"
    assert r["b"].error["message"] == "it broke"
    assert r["c"].status == "skipped"
    assert r["d"].status == "skipped"
    assert "critical stage 'b' failed" in r["d"].skipped_because

    status = overall_status(list(r.values()))
    assert status == "failed"
    assert exit_code(status) != 0


def test_failed_non_critical_stage_gives_partial_and_the_other_branch_still_runs():
    stages = [
        Stage("prep", ok, critical=True),
        Stage("one", boom, needs=("prep",)),
        Stage("one_out", ok, needs=("one",)),
        Stage("two", ok, needs=("prep",)),
        Stage("two_out", ok, needs=("two",)),
    ]
    r = by_name(run_stages(stages, ctx=None))

    assert r["one"].status == "failed"
    assert r["one_out"].status == "skipped"
    assert r["two_out"].status == "success"

    status = overall_status(list(r.values()))
    assert status == "partial"
    assert exit_code(status) != 0


def test_when_nothing_non_critical_succeeded_it_is_failed_not_partial():
    stages = [
        Stage("prep", ok, critical=True),
        Stage("one", boom, needs=("prep",)),
        Stage("two", boom, needs=("prep",)),
    ]
    assert overall_status(run_stages(stages, ctx=None)) == "failed"


def test_prerequisite_check_failure_stops_the_stage_before_it_runs():
    called = []

    def check(ctx):
        raise PrerequisiteError("view is stale")

    def work(ctx):
        called.append(1)

    r = run_stages([Stage("export", work, check=check)], ctx=None)[0]

    assert r.status == "failed"
    assert r.error["type"] == "PrerequisiteError"
    assert called == []


def test_a_stage_that_needs_something_later_in_the_list_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        run_stages([Stage("a", ok, needs=("b",)), Stage("b", ok)], ctx=None)


def test_critical_failure_after_a_successful_non_critical_stage_is_still_failed():
    # the case the "critical" rule exists for: something optional already worked,
    # then something critical broke. That must not be softened to "partial".
    stages = [
        Stage("optional", ok),
        Stage("important", boom, critical=True),
    ]
    results = run_stages(stages, ctx=None)

    assert by_name(results)["optional"].status == "success"
    status = overall_status(results)
    assert status == "failed"
    assert exit_code(status) == 1
