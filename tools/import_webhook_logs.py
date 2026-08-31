#!/usr/bin/env python3
"""Import a Buyer-Monitor webhook-log CSV into a client's MarketBase.

The webhook log is the record of every person we pushed to the client's system
because they newly connected to a competitor rep. One row = one webhook event:

    Log ID, Log Created At, Existing Webhook Sent At, New Webhook Sent At,
    Person Name, Person LinkedIn URL, Person Headline, Person Company,
    Target Name, Target LinkedIn URL, Target Company

Writes straight to Postgres, no disk artifacts:
  * `leads`          — upsert the Person (COALESCE-fill, never clobbers curated data)
  * `lead_sources`   — one row per webhook event, source_date = the date it was sent,
                       raw_data = the whole CSV row (so the competitor rep who
                       triggered it stays attributable)
  * `lead_tags`      — the webhooked tag, plus an optional per-file batch tag

Idempotent on `Log ID`: a re-run of the same CSV (or an overlapping export) inserts
nothing new. Person Company falls back to parsing "<title> at <Company>" out of the
headline when the column is blank.

Usage:
  python3 import_webhook_logs.py --client Acme \
      --csv ".../webhook-logs-acme-2026-08-12.csv" \
      [--batch-tag webhook:2026-08-12] [--dry-run]
"""
from __future__ import annoacmens

import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import connect, normalize_linkedin_url, linkedin_urn, resolve_canonical_url  # noqa: E402

from psycopg.types.json import Jsonb  # noqa: E402

DEFAULT_SOURCE_TYPE = "buyer_monitor_likely_to_connect"
DEFAULT_TAG = "comp_intel:webhooked"

_AT_RE = re.compile(r"\s+at\s+(.+)$", re.I)


def company_from_row(row: dict) -> str:
    """Person Company, else the tail of '<title> at <Company>' in the headline."""
    co = (row.get("Person Company") or "").strip()
    if co:
        return co
    m = _AT_RE.search((row.get("Person Headline") or "").strip())
    return m.group(1).strip() if m else ""


def title_from_headline(headline: str) -> str:
    """The part before ' at ' — a usable current_title when we have nothing else."""
    h = (headline or "").strip()
    m = _AT_RE.search(h)
    return h[: m.start()].strip() if m else h


def sent_date(row: dict) -> date | None:
    raw = (row.get("New Webhook Sent At") or row.get("Existing Webhook Sent At") or "").strip()
    if not raw:
        raw = (row.get("Log Created At") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--source-type", default=DEFAULT_SOURCE_TYPE)
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--batch-tag", default="", help="extra tag for this file, e.g. webhook:2026-08-12")
    ap.add_argument("--source-label", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = Path(args.csv)
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    label = args.source_label or f"comp intel webhook log ({path.name})"
    print(f"{path.name}: {len(rows)} rows")

    stats = {"seen": 0, "skipped_no_url": 0, "already_logged": 0,
             "leads_new": 0, "leads_updated": 0, "sources": 0, "tags": 0}

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            # Guard the FK before we start (an unregistered source_type fails silently
            # in some ingest paths and leaves the count at 0).
            cur.execute("SELECT 1 FROM source_types WHERE name = %s", (args.source_type,))
            if not cur.fetchone():
                sys.exit(f"source_type '{args.source_type}' is not registered in "
                         f"{args.client}'s source_types. Run marketbase-migrate-all-clients first.")

            # Every Log ID already ingested, so a re-run is a no-op.
            cur.execute("""SELECT raw_data->>'Log ID' FROM lead_sources
                           WHERE source_type = %s AND raw_data ? 'Log ID'""",
                        (args.source_type,))
            done = {r[0] for r in cur.fetchall() if r[0]}
            print(f"already ingested Log IDs: {len(done)}")

            for i, row in enumerate(rows, 1):
                stats["seen"] += 1
                log_id = (row.get("Log ID") or "").strip()
                if log_id and log_id in done:
                    stats["already_logged"] += 1
                    continue

                url = normalize_linkedin_url((row.get("Person LinkedIn URL") or "").strip())
                if not url:
                    stats["skipped_no_url"] += 1
                    continue
                if args.dry_run:
                    continue

                url = resolve_canonical_url(cur, url)
                headline = (row.get("Person Headline") or "").strip()
                cur.execute("""
                    INSERT INTO leads (linkedin_url, linkedin_urn, name, headline,
                                       current_title, current_company, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (linkedin_url) DO UPDATE SET
                        name            = COALESCE(NULLIF(leads.name, ''),            EXCLUDED.name),
                        headline        = COALESCE(NULLIF(leads.headline, ''),        EXCLUDED.headline),
                        current_title   = COALESCE(NULLIF(leads.current_title, ''),   EXCLUDED.current_title),
                        current_company = COALESCE(NULLIF(leads.current_company, ''), EXCLUDED.current_company),
                        updated_at      = NOW()
                    RETURNING id, (xmax = 0) AS inserted
                """, (url, linkedin_urn(url), (row.get("Person Name") or "").strip(),
                      headline, title_from_headline(headline), company_from_row(row)))
                lead_id, inserted = cur.fetchone()
                stats["leads_new" if inserted else "leads_updated"] += 1

                cur.execute("""INSERT INTO lead_sources
                                 (lead_id, source_type, source_label, source_date, raw_data)
                               VALUES (%s, %s, %s, %s, %s)""",
                            (lead_id, args.source_type, label, sent_date(row), Jsonb(row)))
                stats["sources"] += 1

                for tag in [args.tag] + ([args.batch_tag] if args.batch_tag else []):
                    cur.execute("""INSERT INTO lead_tags (lead_id, tag, notes, tagged_by)
                                   VALUES (%s, %s, %s, 'import_webhook_logs')
                                   ON CONFLICT (lead_id, tag) DO NOTHING""",
                                (lead_id, tag, path.name))
                    stats["tags"] += cur.rowcount

                if log_id:
                    done.add(log_id)
                if i % 100 == 0:
                    conn.commit()          # flush per batch, so a crash keeps the work
                    print(f"  {i}/{len(rows)}…", flush=True)
        conn.commit()

    print("\n=== import summary ===")
    for k, v in stats.items():
        print(f"  {k:>16}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
