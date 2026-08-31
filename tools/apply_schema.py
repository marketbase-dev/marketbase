#!/usr/bin/env python3
"""Apply MarketBase migrations, skipping any already recorded.

This is the supported way to bring a database up to date. It records each
applied file in `schema_migrations` and never re-runs one.

    python3 tools/apply_schema.py                     # uses MARKETBASE_URL
    python3 tools/apply_schema.py --instance acme
    python3 tools/apply_schema.py --url postgresql://...
    python3 tools/apply_schema.py --status            # report without applying

Exit code is 0 on success, 1 on failure. Applying twice is a no-op, which CI
asserts on every push.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"


def _connect(url: str):
    try:
        import psycopg
        return psycopg.connect(url, autocommit=True)
    except ImportError:
        pass
    try:
        import psycopg2
        c = psycopg2.connect(url); c.autocommit = True
        return c
    except ImportError:
        sys.exit("Need psycopg (pip install 'psycopg[binary]') or psycopg2.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="Postgres URL. Overrides every other source.")
    ap.add_argument("--instance", help="Named MarketBase instance.")
    ap.add_argument("--status", action="store_true", help="Report state, apply nothing.")
    args = ap.parse_args()

    url = args.url or os.environ.get("MARKETBASE_URL")
    if not url:
        try:
            from lib import secrets, instances
            url = secrets.database_url(instances.resolve(args.instance))
        except Exception as e:
            return _die(f"No database URL. Pass --url, set MARKETBASE_URL, "
                        f"or configure an instance.\n({e})")

    files = sorted(p for p in SCHEMA_DIR.glob("*.sql"))
    if not files:
        return _die(f"No migrations found in {SCHEMA_DIR}")

    conn = _connect(url)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                     filename text PRIMARY KEY,
                     applied_at timestamptz NOT NULL DEFAULT now())""")
    cur.execute("SELECT filename FROM schema_migrations")
    done = {r[0] for r in cur.fetchall()}

    pending = [f for f in files if f.name not in done]
    if args.status:
        print(f"applied: {len(done)}  pending: {len(pending)}")
        for f in pending:
            print(f"  pending  {f.name}")
        return 0

    applied = 0
    for f in files:
        if f.name in done:
            continue
        try:
            cur.execute(f.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s) "
                        "ON CONFLICT DO NOTHING", (f.name,))
            applied += 1
            print(f"  applied  {f.name}")
        except Exception as e:
            return _die(f"{f.name} failed: {e}")

    print(f"applied {applied}, skipped {len(files) - applied}")
    # Machine-readable, so CI can assert the second run is a no-op.
    print(f"MARKETBASE_APPLIED={applied}")
    return 0


def _die(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
