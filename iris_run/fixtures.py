import csv
from datetime import date
from pathlib import Path

from . import freshness

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _text(value):
    value = (value or "").strip()
    return value or None


def _int(value):
    value = _text(value)
    return int(value) if value else None


def _date(value):
    value = _text(value)
    return date.fromisoformat(value) if value else None


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_fixtures(conn, directory=FIXTURES_DIR):
    """
    Staging is a landing table, so it is wiped and refilled.
    Substations are upserted, and only count as a data change if a row really differs.
    """
    directory = Path(directory)
    parcels = _read(directory / "parcels.csv")
    substations = _read(directory / "substations.csv")

    with conn.transaction():
        conn.execute("TRUNCATE staging.parcel_raw RESTART IDENTITY")
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO staging.parcel_raw
                    (country_code, source_id, name, land_type, srid, source_date, review_status, wkt)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        _text(r["country_code"]), _text(r["source_id"]), _text(r["name"]),
                        _text(r["land_type"]), _int(r["srid"]), _date(r["source_date"]),
                        _text(r["review_status"]) or "pending", _text(r["wkt"]),
                    )
                    for r in parcels
                ],
            )

        changed = 0
        for r in substations:
            cur = conn.execute(
                """
                INSERT INTO core.substation AS t
                    (country_code, substation_id, name, voltage_kv, source_date, geom)
                VALUES (%s, %s, %s, %s, %s, ST_Transform(ST_GeomFromText(%s::text, %s::int), 4326))
                ON CONFLICT (country_code, substation_id) DO UPDATE
                    SET name = EXCLUDED.name, voltage_kv = EXCLUDED.voltage_kv,
                        source_date = EXCLUDED.source_date, geom = EXCLUDED.geom
                    WHERE (t.name, t.voltage_kv, t.source_date, ST_AsBinary(t.geom))
                          IS DISTINCT FROM
                          (EXCLUDED.name, EXCLUDED.voltage_kv, EXCLUDED.source_date, ST_AsBinary(EXCLUDED.geom))
                """,
                (
                    _text(r["country_code"]), _text(r["substation_id"]), _text(r["name"]),
                    _int(r["voltage_kv"]), _date(r["source_date"]), _text(r["wkt"]), _int(r["srid"]),
                ),
            )
            changed += cur.rowcount

        if changed:
            freshness.touch(conn, "core_data")

    return {"parcel_rows": len(parcels), "substation_rows": len(substations), "substations_changed": changed}
