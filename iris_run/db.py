import os

import psycopg
from psycopg.rows import dict_row

# port 5433 so it does not fight with a Postgres you may already run locally
DEFAULT_URL = "postgresql://iris:iris@localhost:5433/iris"


def default_url():
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def connect(url=None):
    # autocommit on, transactions are opened explicitly where they matter
    return psycopg.connect(url or default_url(), autocommit=True, connect_timeout=5)


def query(conn, sql, params=None):
    # small helper: run a select and get a list of dicts back
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()
