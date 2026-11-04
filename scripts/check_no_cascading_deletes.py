#!/usr/bin/env python3
"""Fail if any foreign key in MarketBase cascades, nulls or defaults on delete
(schema/075), and prove that a delete which would orphan history is refused.
Every write runs inside ONE transaction that is always rolled back.

Why this exists: the rule is "nothing deletes anything". 075 converted every
existing key; this keeps a NEW migration from quietly declaring
ON DELETE CASCADE again, which is the default many examples copy.

Usage:
  python3 scripts/check_no_cascading_deletes.py                 # MARKETBASE_URL (CI)
  python3 scripts/check_no_cascading_deletes.py --url postgresql://...
  python3 scripts/check_no_cascading_deletes.py --instance <name>
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

FK_VIOLATION = "23503"


def _connect(args):
    if args.instance:
        import lib
        return lib.connect(args.instance)
    url = args.url or os.environ.get("MARKETBASE_URL")
    if not url:
        sys.exit("No database. Pass --url, --instance, or set MARKETBASE_URL.")
    try:
        import psycopg
        return psycopg.connect(url, autocommit=False)
    except ImportError:
        import psycopg2
        conn = psycopg2.connect(url)
        conn.autocommit = False
        return conn


def run(conn) -> tuple[int, list[str]]:
    cur = conn.cursor()
    fails: list[str] = []
    total = 0

    def check(name, cond, detail=""):
        nonlocal total
        total += 1
        print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  ' + str(detail)}")
        if not cond:
            fails.append(name)

    cur.execute("""
        SELECT c.conrelid::regclass::text, c.conname, c.confdeltype
          FROM pg_constraint c JOIN pg_namespace ns ON ns.oid = c.connamespace
         WHERE c.contype = 'f' AND ns.nspname = 'public' AND c.confdeltype IN ('c', 'n', 'd')
         ORDER BY 1, 2""")
    bad = cur.fetchall()
    check("no foreign key cascades, nulls or defaults on delete", bad == [], bad)

    cur.execute("SELECT count(*) FROM pg_constraint c JOIN pg_namespace ns ON ns.oid = c.connamespace "
                "WHERE c.contype = 'f' AND ns.nspname = 'public'")
    check("foreign keys still exist (the conversion did not drop them)", cur.fetchone()[0] > 0)

    # behaviour: a lead that has history cannot be deleted
    cur.execute("INSERT INTO leads (linkedin_url, name) VALUES "
                "('https://www.linkedin.com/in/zz-no-cascade', 'ZZ NoCascade') RETURNING id")
    lead = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM lead_identities WHERE lead_id = %s", (lead,))
    has_identity = cur.fetchone()[0] > 0
    cur.execute("SAVEPOINT d")
    state = None
    try:
        cur.execute("DELETE FROM leads WHERE id = %s", (lead,))
    except Exception as e:  # noqa: BLE001 - the error IS the result
        state = getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)
    cur.execute("ROLLBACK TO SAVEPOINT d")
    check("deleting a lead that holds identities is refused",
          has_identity and state == FK_VIOLATION, f"identity={has_identity} sqlstate={state}")
    cur.execute("SELECT count(*) FROM lead_identities WHERE lead_id = %s", (lead,))
    check("its identity rows are still there", cur.fetchone()[0] > 0)
    return total, fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="Postgres URL. Overrides MARKETBASE_URL.")
    ap.add_argument("--instance", help="Named MarketBase instance (written, then rolled back).")
    args = ap.parse_args()
    conn = _connect(args)
    try:
        total, fails = run(conn)
    finally:
        conn.rollback()
        conn.close()
        print("rolled back: nothing was written")
    if fails:
        print(f"check_no_cascading_deletes: {len(fails)} of {total} FAILED: {fails}")
        return 1
    print(f"check_no_cascading_deletes: {total} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
