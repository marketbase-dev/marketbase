#!/usr/bin/env python3
"""Exercise the voice policy function rule (schema/077) against a database that
already has every migration applied. Everything runs inside ONE transaction that
is always rolled back: no row survives, on any instance.

Why this exists: which functions' leaders we follow is a rule in the instance,
checked by the database. This proves the house default was seeded once per
rule-picked group, the withdrawn thresholds are gone, and a function outside the
vocabulary or a negative traction cutoff is refused.

Usage:
  python3 scripts/check_voice_policy_functions.py                 # MARKETBASE_URL (CI)
  python3 scripts/check_voice_policy_functions.py --url postgresql://...
  python3 scripts/check_voice_policy_functions.py --instance <name>
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

CHECK, UNIQUE, FK = "23514", "23505", "23503"
IN = "https://www.linkedin.com/in/"
URN = "ACoAA" + "TEST" * 8 + "09"   # shaped like a member URN, low entropy


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


def _sqlstate(e) -> str | None:
    return getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)


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

    def attempt(sql, params=()):
        cur.execute("SAVEPOINT v")
        try:
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
            cur.execute("RELEASE SAVEPOINT v")
            return None, row
        except Exception as e:  # noqa: BLE001 - the error IS the result
            cur.execute("ROLLBACK TO SAVEPOINT v")
            return _sqlstate(e), None

    def refused(name, sql, params=(), code=CHECK):
        state, _ = attempt(sql, params)
        check(name, state == code, f"got sqlstate {state}")

    def one(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchone()

    DEFAULT = ["executive", "sales", "marketing", "product", "partnerships", "solutions",
               "customer_success", "strategy"]
    cur.execute("SELECT voice_group, keep_functions, workspace_keep_functions, min_post_reactions "
                "FROM voice_policy WHERE version = 'house-default@2026-11-04' ORDER BY voice_group")
    seeded = cur.fetchall()
    check("the house default is seeded for competitor and own, not industry",
          [r[0] for r in seeded] == ["competitor", "own"], seeded)
    check("the default keeps executives and the market-facing functions",
          all(list(r[1]) == DEFAULT and r[2] is None and r[3] is None for r in seeded), seeded)
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'voice_policy' "
                "AND column_name IN ('post_recency_days', 'min_target_reposts', 'min_finance_share')")
    check("the three withdrawn thresholds are gone", cur.fetchall() == [])

    cur.execute("UPDATE voice_policy SET superseded_at = now() WHERE voice_group = 'competitor' "
                "AND version = 'house-default@2026-11-04' AND superseded_at IS NULL")
    check("superseding the competitor default is one row", cur.rowcount == 1, cur.rowcount)
    pol = ("INSERT INTO voice_policy (voice_group, version, keep_functions, workspace_keep_functions, "
           "min_post_reactions, source, rationale, created_by) "
           "VALUES ('competitor', %s, %s, %s, %s, 'zz source', 'zz why', 'check_voice_policy_functions')")
    refused("an unknown default function is refused", pol, ("zz-v1", DEFAULT + ["astrology"], None, None))
    refused("an unknown workspace function is refused", pol, ("zz-v1", DEFAULT, ["astrology"], None))
    refused("a negative traction cutoff is refused", pol, ("zz-v1", DEFAULT, ["finance"], -1))
    state, _ = attempt(pol, ("zz-v1", DEFAULT, ["finance"], 5))
    check("a workspace version adding finance with a cutoff is accepted", state is None, state)
    state, _ = attempt(pol, ("zz-v2", DEFAULT, ["security_it"], None))
    check("a second active competitor policy still collides", state == UNIQUE, state)

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
        print(f"check_voice_policy_functions: {len(fails)} of {total} FAILED: {fails}")
        return 1
    print(f"check_voice_policy_functions: {total} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
