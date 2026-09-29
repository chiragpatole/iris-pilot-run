import os

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from iris_run.db import connect
from iris_run.fixtures import load_fixtures
from iris_run.migrate import migrate

ADMIN_URL = os.environ.get("ADMIN_DATABASE_URL", "postgresql://iris:iris@localhost:5433/iris")
TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://iris:iris@localhost:5433/iris_test")


@pytest.fixture(scope="session")
def test_db_url():
    """Makes sure a separate iris_test database exists, so tests never touch your real one."""
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.fail(
            f"cannot reach the database ({exc}). Start it first with: docker compose up -d --wait",
            pytrace=False,
        )
    name = conninfo_to_dict(TEST_URL)["dbname"]
    with admin:
        if not admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            admin.execute(f'CREATE DATABASE "{name}"')
    return TEST_URL


@pytest.fixture
def db(test_db_url):
    """A clean, migrated database with the fixtures loaded, fresh for every test."""
    conn = connect(test_db_url)
    conn.execute("DROP SCHEMA IF EXISTS staging, core, mart, ops CASCADE")
    migrate(conn)
    load_fixtures(conn)
    yield conn
    conn.close()
