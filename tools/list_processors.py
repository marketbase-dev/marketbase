#!/usr/bin/env python3
"""marketbase-list-processors

List registered processors in a client's MarketBase. Filter by --type or --name.
By default shows only the current (non-superseded) version of each process;
pass --all to include historical versions.

Usage:
  python3 list_processors.py --client Acme-AI
  python3 list_processors.py --client Acme-AI --type classifier
  python3 list_processors.py --client Acme-AI --name demand_gen_persona_classifier --all
"""
from __future__ import annoacmens

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect


def main() -> int:
    parser = argparse.ArgumentParser(description="List processors in the MarketBase registry.")
    parser.add_argument("--client", required=True)
    parser.add_argument("--type", choices=("classifier", "fetcher", "reporter", "orchestrator", "enricher"),
                        help="Filter to one processor_type.")
    parser.add_argument("--name", help="Filter to one process_name.")
    parser.add_argument("--all", action="store_true",
                        help="Include superseded historical versions.")
    args = parser.parse_args()

    where = []
    params = []
    if not args.all:
        where.append("superseded_by IS NULL")
    if args.type:
        where.append("processor_type = %s")
        params.append(args.type)
    if args.name:
        where.append("name = %s")
        params.append(args.name)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT name, version, processor_type, description, created_at,
                       superseded_by IS NOT NULL AS superseded
                FROM processors
                {where_sql}
                ORDER BY name, created_at DESC
            """, tuple(params))
            rows = cur.fetchall()

    if not rows:
        print("(no processors registered)")
        return 0

    name_w = max(8, max(len(r[0]) for r in rows))
    ver_w  = max(8, max(len(r[1]) for r in rows))
    type_w = 11
    print(f"{'name':<{name_w}}  {'version':<{ver_w}}  {'type':<{type_w}}  status      description")
    print("-" * (name_w + ver_w + type_w + 70))
    for name, version, ptype, desc, created, superseded in rows:
        status = "SUPERSEDED" if superseded else "active"
        desc_short = (desc or "")[:60]
        print(f"{name:<{name_w}}  {version:<{ver_w}}  {ptype:<{type_w}}  {status:<10}  {desc_short}")
    print(f"\n{len(rows)} processor(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
