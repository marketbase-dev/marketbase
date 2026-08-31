#!/usr/bin/env python3
"""Export buying-committee expansion candidates from a client's MarketBase to CSV.

One row per person, carrying everything an SDR needs to make the call without
opening the database: who they are, where they work, which already-eligible
colleague triggered the expansion, and WHICH COMPETITOR that colleague connected
to. The competitor is the whole reason the account surfaced, so it belongs in
the file rather than three joins away.

Tiers (set by tier_expansion_candidates.py):
  expansion:qualified  the hand-over set, top N per company
  expansion:backup     the researched bench beyond N at the same company

Usage:
  python3 export_expansion_csv.py --client Acme [--tier qualified|backup|all]
      [--output <path.csv>]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import connect  # noqa: E402

DRIVE = Path.home() / ("Library/CloudStorage/GoogleDrive-you@example.com/My Drive"
                       "/Impact11/Customers and Partners")

SQL = """
WITH cand AS (
    SELECT DISTINCT ON (l.id)
           l.id, l.name, l.current_title, l.current_company, l.current_company_url,
           l.linkedin_url, l.email, l.city, l.country, l.headline,
           s.raw_data->>'rank'          AS rank,
           s.raw_data->>'matched_title' AS matched_title,
           s.raw_data->>'seed_company'  AS seed_company,
           s.source_date                AS discovered_on,
           (SELECT string_agg(DISTINCT u.value, ' | ')
              FROM jsonb_array_elements_text(s.raw_data->'seed_linkedin_urls') u) AS seed_urls
    FROM leads l
    JOIN lead_tags t    ON t.lead_id = l.id AND t.tag = ANY(%(tiers)s)
    JOIN lead_sources s ON s.lead_id = l.id AND s.source_type = 'buying_committee_expansion'
    ORDER BY l.id, s.recorded_at DESC
),
bench AS (
    SELECT current_company, count(*) AS n FROM cand GROUP BY 1
)
SELECT
    CASE WHEN EXISTS (SELECT 1 FROM lead_tags q
                      WHERE q.lead_id = cand.id AND q.tag = 'expansion:qualified')
         THEN 'qualified' ELSE 'backup' END              AS tier,
    cand.name, cand.current_title, cand.rank,
    cand.current_company, cand.current_company_url,
    co.website, co.industry, co.employee_count,
    cand.linkedin_url, cand.email, cand.city, cand.country, cand.headline,
    bench.n                                              AS candidates_at_company,
    cand.seed_company, cand.seed_urls,
    (SELECT string_agg(DISTINCT sl.name, ' | ') FROM leads sl
      WHERE sl.linkedin_url = ANY(string_to_array(cand.seed_urls, ' | ')))   AS seed_name,
    (SELECT string_agg(DISTINCT sl.current_title, ' | ') FROM leads sl
      WHERE sl.linkedin_url = ANY(string_to_array(cand.seed_urls, ' | ')))   AS seed_title,
    (SELECT max(ws.source_date) FROM lead_sources ws
       JOIN leads sl ON sl.id = ws.lead_id
      WHERE ws.source_type = 'buyer_monitor_likely_to_connect'
        AND sl.linkedin_url = ANY(string_to_array(cand.seed_urls, ' | ')))   AS seed_webhooked_on,
    (SELECT string_agg(DISTINCT ws.raw_data->>'Target Company', ' | ') FROM lead_sources ws
       JOIN leads sl ON sl.id = ws.lead_id
      WHERE ws.source_type = 'buyer_monitor_likely_to_connect'
        AND sl.linkedin_url = ANY(string_to_array(cand.seed_urls, ' | ')))   AS competitors_connected,
    (SELECT string_agg(DISTINCT ws.raw_data->>'Target Name', ' | ') FROM lead_sources ws
       JOIN leads sl ON sl.id = ws.lead_id
      WHERE ws.source_type = 'buyer_monitor_likely_to_connect'
        AND sl.linkedin_url = ANY(string_to_array(cand.seed_urls, ' | ')))   AS competitor_reps,
    cand.discovered_on, cand.matched_title
FROM cand
LEFT JOIN bench ON bench.current_company = cand.current_company
-- companies.linkedin_url is NOT unique (the unique key is linkedin_slug), so a
-- plain join here silently duplicates rows for any company stored twice.
LEFT JOIN (SELECT DISTINCT ON (linkedin_url) linkedin_url, website, industry, employee_count
             FROM companies WHERE linkedin_url IS NOT NULL AND linkedin_url <> ''
            ORDER BY linkedin_url, employee_count DESC NULLS LAST) co
       ON co.linkedin_url = cand.current_company_url
ORDER BY tier DESC, cand.current_company, cand.rank, cand.name
"""

TIERS = {"qualified": ["expansion:qualified"],
         "backup": ["expansion:backup"],
         "all": ["expansion:qualified", "expansion:backup"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--tier", choices=list(TIERS), default="qualified")
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    out = Path(args.output) if args.output else (
        DRIVE / args.client / f"{args.client} GTM"
        / f"{args.client} buying committee - {args.tier} - {date.today()}.csv")

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL, {"tiers": TIERS[args.tier]})
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()

    if not rows:
        print(f"No {args.tier} expansion candidates found for {args.client}.")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)

    tier_i, co_i = cols.index("tier"), cols.index("current_company")
    print(f"wrote {len(rows)} rows -> {out}")
    print(f"  companies      : {len({r[co_i] for r in rows})}")
    print(f"  qualified      : {sum(1 for r in rows if r[tier_i] == 'qualified')}")
    print(f"  backup         : {sum(1 for r in rows if r[tier_i] == 'backup')}")
    print(f"  columns        : {len(cols)}")
    for missing in ("email", "employee_count", "competitors_connected"):
        i = cols.index(missing)
        filled = sum(1 for r in rows if r[i] not in (None, ""))
        print(f"  {missing:<15}: {filled}/{len(rows)} populated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
