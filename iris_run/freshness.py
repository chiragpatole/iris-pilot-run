from datetime import timezone

from .db import query

VERTICALS = ("bess", "peatland")

# the order matters: each item is built from the one before it
ITEMS = ["core_data"] + [f"view:{v}" for v in VERTICALS] + [f"export:{v}" for v in VERTICALS]


def touch(conn, item):
    # clock_timestamp() and not now(): now() is frozen at the start of the
    # transaction, which would make things built inside one run look simultaneous
    conn.execute(
        """
        INSERT INTO ops.freshness (item, updated_at) VALUES (%s, clock_timestamp())
        ON CONFLICT (item) DO UPDATE SET updated_at = EXCLUDED.updated_at
        """,
        (item,),
    )


def _fmt(ts):
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def report(conn):
    """
    One entry per item: when it was last built, whether it is stale, and why.
    A view is stale if the promoted data changed after it was refreshed.
    An export is stale if its view is stale, or the view was refreshed after it was written.
    """
    times = {r["item"]: r["updated_at"] for r in query(conn, "SELECT item, updated_at FROM ops.freshness")}
    data_time = times.get("core_data")
    result = {}

    def entry(item, stale, reason=None):
        ts = times.get(item)
        result[item] = {
            "item": item,
            "updated_at": ts.isoformat() if ts else None,
            "stale": stale,
            "reason": reason,
        }

    if data_time is None:
        entry("core_data", True, "nothing has been promoted yet")
    else:
        entry("core_data", False)

    for v in VERTICALS:
        view_time = times.get(f"view:{v}")
        if view_time is None:
            entry(f"view:{v}", True, "never refreshed")
        elif data_time is not None and view_time < data_time:
            entry(f"view:{v}", True, f"data changed {_fmt(data_time)}, view last refreshed {_fmt(view_time)}")
        else:
            entry(f"view:{v}", False)

    for v in VERTICALS:
        export_time = times.get(f"export:{v}")
        view = result[f"view:{v}"]
        if export_time is None:
            entry(f"export:{v}", True, "never exported")
        elif view["stale"]:
            entry(f"export:{v}", True, f"built from a stale view ({view['reason']})")
        elif export_time < times[f"view:{v}"]:
            entry(f"export:{v}", True, f"view refreshed {_fmt(times[f'view:{v}'])}, dossiers written {_fmt(export_time)}")
        else:
            entry(f"export:{v}", False)

    return [result[item] for item in ITEMS]
