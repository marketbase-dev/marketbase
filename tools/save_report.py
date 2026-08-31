#!/usr/bin/env python3
"""marketbase-save-report

Register a named SQL query (optionally wrapped as a Postgres VIEW) with
metadata explaining what it does and why we created it. Idempotent — if a
report with the same name exists, it is updated (and last_run_at preserved).

Usage:
  # Save a query inline
  python3 save_report.py --client Acme-AI \
    --name top-engagers \
    --description "Engagers ranked by total engagement across all leaders" \
    --purpose "Used to triage who to invite next" \
    --sql 'SELECT * FROM v_lead_pipeline_state WHERE tags && ARRAY['"'"'qualified-creator'"'"']'

  # Save from a .sql file and also create a VIEW
  python3 save_report.py --client Acme-AI \
    --name thought-leader-engager-report \
    --sql-file ~/reports/tl_engager_report.sql \
    --as-view v_thought_leader_engager_report_named \
    --purpose "Per-leader engager-persona tally; refreshed 2026-05-25"
"""
from __future__ import annoacmens

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect


def main() -> int:
    parser = argparse.ArgumentParser(description="Register a saved report.")
    parser.add_argument("--client", required=True)
    parser.add_argument("--name", required=True, help="Unique report name.")
    parser.add_argument("--sql", help="The SQL query (inline).")
    parser.add_argument("--sql-file", help="Path to a .sql file with the query.")
    parser.add_argument("--description")
    parser.add_argument("--purpose", help="Why we created this report (analyst note).")
    parser.add_argument("--created-by", default=None)
    parser.add_argument("--as-view", metavar="VIEW_NAME",
                        help="Also wrap the SQL in a CREATE OR REPLACE VIEW with the given name.")
    args = parser.parse_args()

    if not args.sql and not args.sql_file:
        sys.exit("--sql or --sql-file is required")
    if args.sql and args.sql_file:
        sys.exit("pass only one of --sql / --sql-file")

    sql_query = args.sql if args.sql else Path(args.sql_file).read_text()
    sql_query = sql_query.strip().rstrip(";")  # we'll re-add semis when needed

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            if args.as_view:
                # Strict identifier validation — no quoting tricks.
                if not args.as_view.replace("_", "").isalnum():
                    sys.exit(f"invalid view name {args.as_view!r}; use [a-zA-Z0-9_]")
                cur.execute(f"CREATE OR REPLACE VIEW {args.as_view} AS {sql_query}")

            cur.execute("""
                INSERT INTO saved_reports
                  (name, description, purpose, sql_query, view_name, created_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                  description = EXCLUDED.description,
                  purpose     = EXCLUDED.purpose,
                  sql_query   = EXCLUDED.sql_query,
                  view_name   = EXCLUDED.view_name,
                  created_by  = COALESCE(EXCLUDED.created_by, saved_reports.created_by)
                RETURNING id, (xmax = 0) AS is_insert
            """, (args.name, args.description, args.purpose, sql_query,
                  args.as_view, args.created_by))
            row = cur.fetchone()
            verb = "created" if row[1] else "updated"
        conn.commit()

    print(f"✓ {verb} saved report: {args.name}")
    if args.as_view:
        print(f"  view: {args.as_view}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
