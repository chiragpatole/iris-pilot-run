from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def migration_files():
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def applied_migrations(conn):
    # before the first migrate the bookkeeping table does not exist yet
    exists = conn.execute("SELECT to_regclass('ops.schema_migration')").fetchone()[0]
    if exists is None:
        return set()
    return {row[0] for row in conn.execute("SELECT name FROM ops.schema_migration")}


def migrate(conn):
    """Apply every migration file that has not been applied yet. Safe to repeat."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS ops")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops.schema_migration (
            name       text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    done = applied_migrations(conn)
    newly_applied = []
    for path in migration_files():
        if path.name in done:
            continue
        # one transaction per file, so a broken migration leaves nothing half-applied
        with conn.transaction():
            conn.execute(path.read_text())
            conn.execute("INSERT INTO ops.schema_migration (name) VALUES (%s)", (path.name,))
        newly_applied.append(path.name)
    return newly_applied
