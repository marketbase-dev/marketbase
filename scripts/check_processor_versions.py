#!/usr/bin/env python3
"""Prove that a new processor version replaces the one before it, and that the
repair tool fixes rows saved before that was true. Every write runs inside ONE
transaction that is always rolled back.

What it proves:
  * lib._supersede_prior_versions (what register_processor_from_yaml calls on a
    new version) points each earlier current version at the new one, so
    a -> b -> c leaves exactly one current version, and touches no other name;
  * register_processor_from_yaml itself calls it on a new version, not on a
    re-registered one, and not when supersede=False;
  * repair_processor_versions.plan lists every version except the newest when
    several are current, each replaced by the version created right after it,
    and lists nothing once they are marked.

Usage:
  python3 scripts/check_processor_versions.py                 # MARKETBASE_URL (CI)
  python3 scripts/check_processor_versions.py --url postgresql://...
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

NAME, OTHER, STALE, WIRED = ("zz-check-versions", "zz-check-versions-other", "zz-check-versions-stale",
                             "zz-check-versions-wired")


def _insert(cur, name, version, minutes=0):
    cur.execute("""INSERT INTO processors (name, version, processor_type, description, yaml_spec, created_at)
                   VALUES (%s, %s, 'reporter', 'check fixture, rolled back', '', now() + make_interval(mins => %s))
                   RETURNING id""", (name, version, minutes))
    return cur.fetchone()[0]


def run(conn):
    import lib
    import repair_processor_versions as repair
    cur = conn.cursor()
    results = []

    def check(label, ok):
        results.append((label, ok))
        print(("  ok   " if ok else "  FAIL ") + label)

    # 1. the helper: a -> b -> c
    other = _insert(cur, OTHER, "a")
    ids = {}
    marked = []
    for i, v in enumerate("abc"):
        ids[v] = _insert(cur, NAME, v, i)
        marked.append(lib._supersede_prior_versions(cur, NAME, ids[v]))
    check("each new version marks exactly the one before it", marked == [0, 1, 1])
    cur.execute("SELECT version, superseded_by FROM processors WHERE name = %s", (NAME,))
    got = dict(cur.fetchall())
    check("a is replaced by b, b by c, c is current", got == {"a": ids["b"], "b": ids["c"], "c": None})
    cur.execute("SELECT superseded_by FROM processors WHERE id = %s", (other,))
    check("a processor of another name is untouched", cur.fetchone()[0] is None)

    # 1b. the public entry point is wired to it. connect() is swapped for this
    #     transaction (commit does nothing), so the rows vanish with the rollback.
    class _SameTransaction:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return conn.cursor()
        def commit(self): pass

    real_connect, lib.connect = lib.connect, lambda client: _SameTransaction()
    try:
        spec = "name: {n}\nversion: '{v}'\nprocessor_type: reporter\ndescription: check fixture, rolled back\n"
        for v in ("1", "2"):
            lib.register_processor_from_yaml("unused", spec.format(n=WIRED, v=v))
        cur.execute("SELECT version FROM processors WHERE name = %s AND superseded_by IS NULL", (WIRED,))
        check("register_processor_from_yaml leaves only the new version current", [r[0] for r in cur.fetchall()] == ["2"])
        lib.register_processor_from_yaml("unused", spec.format(n=WIRED, v="2"))
        cur.execute("SELECT count(*) FROM processors WHERE name = %s AND superseded_by IS NULL", (WIRED,))
        check("registering the same version again changes no marker", cur.fetchone()[0] == 1)
        lib.register_processor_from_yaml("unused", spec.format(n=WIRED, v="3"), supersede=False)
        cur.execute("SELECT version FROM processors WHERE name = %s AND superseded_by IS NULL ORDER BY version", (WIRED,))
        check("supersede=False keeps the earlier version current", [r[0] for r in cur.fetchall()] == ["2", "3"])
    finally:
        lib.connect = real_connect

    # 2. the repair: three versions all saved as current, the way the old helper left them
    stale = {v: _insert(cur, STALE, v, i) for i, v in enumerate("abc")}
    plan = [r for r in repair.plan(cur) if r["name"] == STALE]
    check("the repair lists every version but the newest",
          [(r["version"], r["by_version"]) for r in plan] == [("a", "b"), ("b", "c")])
    check("the repair does not list the already-correct name", not [r for r in repair.plan(cur) if r["name"] == NAME])
    for r in plan:
        cur.execute("UPDATE processors SET superseded_by = %s WHERE id = %s AND superseded_by IS NULL",
                    (r["by_id"], r["id"]))
    cur.execute("SELECT version FROM processors WHERE name = %s AND superseded_by IS NULL", (STALE,))
    check("after the repair only the newest version is current", [r[0] for r in cur.fetchall()] == ["c"])
    check("a second repair finds nothing", not [r for r in repair.plan(cur) if r["name"] == STALE])
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url")
    args = ap.parse_args()
    url = args.url or os.environ.get("MARKETBASE_URL")
    if not url:
        raise SystemExit("set MARKETBASE_URL or pass --url")
    import psycopg
    conn = psycopg.connect(url, autocommit=False)
    try:
        results = run(conn)
    finally:
        conn.rollback()
        conn.close()
    fails = [label for label, ok in results if not ok]
    print(f"\n{len(results) - len(fails)}/{len(results)} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
