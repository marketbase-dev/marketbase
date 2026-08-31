#!/usr/bin/env python3
"""marketbase-list-reports

Lists saved reports for a client. By default shows only active (not archived).

Usage:
  python3 list_reports.py --client Acme-AI               # active reports
  python3 list_reports.py --client Acme-AI --archived    # archived only
  python3 list_reports.py --client Acme-AI --all         # both
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect


def main() -> int:
    parser = argparse.ArgumentParser(description="List saved reports for a client.")
    parser.add_argument("--client", required=True)
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--archived", action="store_true", help="Show archived reports only.")
    g.add_argument("--all", action="store_true", help="Show both active and archived.")
    args = parser.parse_args()

    if args.archived:
        where = "WHERE archived_at IS NOT NULL"
    elif args.all:
        where = ""
    else:
        where = "WHERE archived_at IS NULL"

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT name, description, purpose, created_by, created_at,
                       last_run_at, archived_at, view_name,
                       (SELECT count(*) FROM saved_report_runs r WHERE r.report_id = sr.id) AS run_count
                FROM saved_reports sr
                {where}
                ORDER BY archived_at NULLS FIRST, created_at DESC
            """)
            rows = cur.fetchall()

    if not rows:
        print("(no reports)")
        return 0

    for r in rows:
        name, desc, purpose, by, created, last_run, archived, view, runs = r
        status = "ARCHIVED" if archived else "active"
        print(f"\n• {name}  [{status}]")
        if desc:    print(f"    description: {desc}")
        if purpose: print(f"    purpose:     {purpose}")
        if view:    print(f"    view:        {view}")
        if by:      print(f"    created by:  {by}")
        print(f"    created:     {created:%Y-%m-%d %H:%M}")
        if last_run:
            print(f"    last run:    {last_run:%Y-%m-%d %H:%M}  ({runs} run(s) total)")
        else:
            print(f"    last run:    never")
        if archived:
            print(f"    archived:    {archived:%Y-%m-%d %H:%M}")

    print(f"\n{len(rows)} report(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
