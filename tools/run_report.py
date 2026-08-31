#!/usr/bin/env python3
"""marketbase-run-report

Executes a saved report, logs the run in saved_report_runs, and prints (or
exports) the result.

Usage:
  # Print to stdout as a table
  python3 run_report.py --client Acme-AI --name thought-leader-engager-report

  # Export to CSV
  python3 run_report.py --client Acme-AI --name top-engagers \
    --output /tmp/top_engagers.csv

  # With a note about why you're running it
  python3 run_report.py --client Acme-AI --name top-engagers \
    --notes "Preparing 2026-06 outreach batch"
"""
from __future__ import annoacmens

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a saved report.")
    parser.add_argument("--client", required=True)
    parser.add_argument("--name", required=True, help="Saved report name.")
    parser.add_argument("--output", help="If set, write CSV to this path instead of stdout table.")
    parser.add_argument("--ran-by", default=None, help="Who/what ran this (e.g. 'alice', 'cron').")
    parser.add_argument("--notes", default=None, help="Note about this particular run.")
    parser.add_argument("--limit", type=int, default=200,
                        help="Limit rows printed to stdout (ignored when --output is given).")
    args = parser.parse_args()

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, sql_query, view_name, archived_at
                FROM saved_reports WHERE name = %s
            """, (args.name,))
            row = cur.fetchone()
            if not row:
                sys.exit(f"No saved report named {args.name!r} (use marketbase-list-reports to see all).")
            report_id, sql_query, view_name, archived_at = row
            if archived_at:
                print(f"⚠ this report is archived (since {archived_at}). Running anyway.\n",
                      file=sys.stderr)

            # Execute. Prefer the view if registered (in case the saved sql_query
            # has drifted from the materialized view definition).
            target = f"SELECT * FROM {view_name}" if view_name else sql_query
            cur.execute(target)
            colnames = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()

            # Log the run
            cur.execute("""
                INSERT INTO saved_report_runs
                  (report_id, row_count, output_path, ran_by, notes)
                VALUES (%s, %s, %s, %s, %s)
            """, (report_id, len(rows), args.output, args.ran_by, args.notes))
            cur.execute("UPDATE saved_reports SET last_run_at = now() WHERE id = %s",
                        (report_id,))
        conn.commit()

    # Output
    if args.output:
        with open(args.output, "w", newline="") as f:
            w = csv.writer(f, quoting=csv.QUOTE_ALL)
            w.writerow(colnames)
            for r in rows:
                w.writerow(["" if v is None else v for v in r])
        print(f"✓ wrote {len(rows)} rows → {args.output}")
    else:
        if not rows:
            print(f"(no rows; report '{args.name}' returned empty)")
            return 0
        # Compact table for terminal
        widths = [max(len(c), max((len(str(r[i] or "")) for r in rows), default=0))
                  for i, c in enumerate(colnames)]
        widths = [min(w, 40) for w in widths]  # cap each column at 40 chars
        header = "  ".join(c[:w].ljust(w) for c, w in zip(colnames, widths))
        print(header)
        print("-" * min(len(header), 200))
        for r in rows[:args.limit]:
            line = "  ".join(str(v or "")[:w].ljust(w) for v, w in zip(r, widths))
            print(line)
        if len(rows) > args.limit:
            print(f"… {len(rows) - args.limit} more rows (use --output to export)")
        print(f"\n{len(rows)} row(s) total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
