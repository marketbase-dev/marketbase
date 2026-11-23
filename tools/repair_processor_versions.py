#!/usr/bin/env python3
"""repair-processor-versions

Mark old versions of a processor replaced when more than one version of the
same name is still marked current.

Why this exists: lib.register_processor_from_yaml used to save a new version
without setting `superseded_by` on the one before it (only
register_processor.py did), so versions saved through it piled up as
"current". Readers that take the newest by created_at were right; any list of
current processors was not. The helper is fixed; this repairs the rows it left.

What it writes: for each name with several current versions, every version
except the newest gets `superseded_by` = the id of the version created right
after it. Nothing is deleted and no other column changes.

Dry run by default. To write, pass --execute and --only with exactly the ids
the dry run listed for that instance: the ids are named by whoever looked at
them, the tool refuses any other set, and each row is written by id.

Usage:
  python3 repair_processor_versions.py --client "Client Name"
  python3 repair_processor_versions.py --all-clients
  python3 repair_processor_versions.py --client "Client Name" --execute --only <id>,<id>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect


def plan(cur) -> list[dict]:
    """Rows to mark replaced: [{id, name, version, created_at, by_id, by_version}]."""
    cur.execute("""SELECT name FROM processors WHERE superseded_by IS NULL
                   GROUP BY name HAVING count(*) > 1 ORDER BY name""")
    out = []
    for (name,) in cur.fetchall():
        cur.execute("""SELECT id, version, created_at FROM processors
                       WHERE name = %s AND superseded_by IS NULL
                       ORDER BY created_at, id""", (name,))
        rows = cur.fetchall()
        for older, newer in zip(rows, rows[1:]):
            out.append({"id": str(older[0]), "name": name, "version": older[1], "created_at": older[2],
                        "by_id": str(newer[0]), "by_version": newer[1]})
    return out


def show(client: str, rows: list[dict]) -> None:
    if not rows:
        print(f"{client}: nothing to repair (one current version per name)")
        return
    print(f"{client}: {len(rows)} version(s) to mark replaced")
    for r in rows:
        print(f"  {r['id']}  {r['name']}@{r['version']}  ({r['created_at']:%Y-%m-%d %H:%M})  "
              f"-> replaced by @{r['by_version']} ({r['by_id']})")


def run(client: str, execute: bool, only: set[str] | None) -> int:
    with connect(client) as conn:
        with conn.cursor() as cur:
            rows = plan(cur)
            show(client, rows)
            if not execute:
                conn.rollback()
                return 0
            planned = {r["id"] for r in rows}
            if only != planned:
                conn.rollback()
                extra, missing = sorted((only or set()) - planned), sorted(planned - (only or set()))
                raise SystemExit(f"{client}: --only must name exactly the ids listed above. "
                                 f"Not in the plan: {extra or 'none'}. Missing: {missing or 'none'}. Nothing written.")
            for r in rows:
                cur.execute("UPDATE processors SET superseded_by = %s WHERE id = %s AND superseded_by IS NULL",
                            (r["by_id"], r["id"]))
                if cur.rowcount != 1:
                    raise RuntimeError(f"{client}: expected 1 row for {r['id']}, got {cur.rowcount}; rolled back")
            # invariants, inside the transaction: one current version per name, and it is the newest one
            cur.execute("SELECT name, count(*) FROM processors WHERE superseded_by IS NULL GROUP BY name HAVING count(*) > 1")
            left = cur.fetchall()
            if left:
                raise RuntimeError(f"{client}: still more than one current version for {left}; rolled back")
            cur.execute("""SELECT p.name FROM processors p
                           WHERE p.superseded_by IS NULL
                             AND p.name = ANY(%s)
                             AND p.created_at < (SELECT max(q.created_at) FROM processors q WHERE q.name = p.name)""",
                        (sorted({r["name"] for r in rows}),))
            wrong = cur.fetchall()
            if wrong:
                raise RuntimeError(f"{client}: the current version is not the newest for {wrong}; rolled back")
        conn.commit()
    print(f"{client}: marked {len(rows)} version(s) replaced")
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Mark old processor versions replaced where several are still current.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--client")
    g.add_argument("--all-clients", action="store_true", help="Dry run over every instance (cannot be combined with --execute).")
    ap.add_argument("--execute", action="store_true", help="Write. Needs --client and --only.")
    ap.add_argument("--only", help="Comma-separated ids to mark replaced; must equal the dry run's list.")
    a = ap.parse_args()

    if a.all_clients:
        if a.execute:
            raise SystemExit("--execute works on one instance at a time: pass --client and --only")
        from migrate_all_clients import discover_clients
        clients = discover_clients()
        if not clients:
            raise SystemExit("no instances found (is the secret store answering?)")
        print(f"{len(clients)} instance(s): {', '.join(clients)}")
        for c in clients:
            run(c, False, None)
        return 0
    if a.execute and not a.only:
        raise SystemExit("--execute needs --only <id>,<id>: run without --execute first and name the ids it lists")
    only = {x.strip() for x in a.only.split(",") if x.strip()} if a.only else None
    run(a.client, a.execute, only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
