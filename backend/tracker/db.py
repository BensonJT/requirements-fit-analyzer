"""PostgreSQL connection and migrations for the application tracker.

The tracker lives in Postgres (database `resume`, schemas `tracker` and `jobs`), never in
DuckDB: the pipeline holds the DuckDB lock for hours. Credentials come from libpq (`~/.pgpass`
or PG* env vars); nothing secret is read or stored here.

    python -m backend.tracker.db migrate [--db NAME]

Connects as $PGUSER (default postgres) on $PGHOST (default localhost).
"""
import argparse
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DEFAULT_DB = "resume"
PG_DIR = Path(__file__).resolve().parents[2] / "db" / "pg"


def connect(dbname=None):
    """Open a connection (host localhost, user postgres) in autocommit mode with dict rows.

    Autocommit means each `with conn.transaction():` block in store.py is a real transaction
    and plain reads never leave a session idle-in-transaction.
    """
    dbname = dbname or os.environ.get("TRACKER_PGDATABASE") or DEFAULT_DB
    return psycopg.connect(
        host=os.environ.get("PGHOST", "localhost"),
        user=os.environ.get("PGUSER", "postgres"),
        dbname=dbname,
        autocommit=True,
        row_factory=dict_row,
    )


def migrate(conn):
    """Apply every db/pg/*.sql in name order. Each file is idempotent. Returns the file names."""
    applied = []
    for path in sorted(PG_DIR.glob("*.sql")):
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
        applied.append(path.name)
    return applied


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m backend.tracker.db", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate", help="apply every db/pg/*.sql in name order, one transaction per file (each is idempotent); "
                                        "stops at the first failing file, earlier files stay applied")
    m.add_argument("--db", default=None, help="database name (default: $TRACKER_PGDATABASE or resume)")
    args = ap.parse_args(argv)
    with connect(args.db) as conn:
        for name in migrate(conn):
            print(f"applied {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
