#!/usr/bin/env python3
"""Select which expanded buying-committee candidates go to outreach, per company.

Discovery (expand_buying_committee.py) is deliberately uncapped, so the whole
committee is researched once. THIS step decides who is actually handed over:

    company with <= CAP candidates  -> every one of them is qualified
    company with  > CAP candidates  -> the top CAP are qualified,
                                       the remainder are held as BACKUP

Keeping the cap out of discovery means the bench is already researched and
attributed when a company needs more names later — promoting a backup costs a
tag update, not another API pass.

WHO MAKES THE TOP CAP
---------------------
Ranked by seniority (c_suite > vp > director > head > controller_family), then by
how CENTRAL the role is to the finance org. A divisional or regional title loses
to the corporate one at the same rank, because "Division CFO - Customer Solutions
Growth Regions" is not the person who buys an FP&A platform; "Chief Financial
Officer" is. FP&A-specific titles get a nudge up, being the closest match to what
the product actually replaces. Ties break on name so runs are deterministic.

Usage:
  python3 tier_expansion_candidates.py --client Acme [--cap 5]
      [--source-type buying_committee_expansion] [--dry-run]
"""
from __future__ import annoacmens

import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import connect  # noqa: E402

RANKS = ["c_suite", "vp", "director", "head", "controller_family"]
RANK_ORDER = {r: i for i, r in enumerate(RANKS)}

# A title scoped to a slice of the business rather than the whole company.
RE_DIVISIONAL = re.compile(
    r"\b(division|divisional|regional|region|plant|site|facility|country|local|"
    r"business\s+unit|segment|emea|apac|eame|latam|noram|americas|europe|asia|"
    r"supply\s+chain|operations|manufacturing|logistics|stores|utilities|"
    r"legal\s+entity|dealer|project|china|mexico|brazil|india|canada|japan|korea|"
    r"uk|australia|germany|france|spain|italy|africa|middle\s+east)\b", re.I)
RE_FPA = re.compile(r"\bfp\s?&\s?a\b|\bfinancial\s+planning\b", re.I)
RE_CORPORATE = re.compile(r"\b(corporate|group|global|chief)\b", re.I)


def sort_key(row) -> tuple:
    """(rank, divisional?, not-corporate?, not-FP&A?, title length, name)."""
    _lead_id, name, title, rank = row
    t = title or ""
    return (RANK_ORDER.get(rank, 99),
            1 if RE_DIVISIONAL.search(t) else 0,
            0 if RE_CORPORATE.search(t) else 1,
            0 if RE_FPA.search(t) else 1,
            len(t),
            (name or "").lower())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--cap", type=int, default=5)
    ap.add_argument("--source-type", default="buying_committee_expansion")
    ap.add_argument("--active-tag", default="expansion:buying_committee")
    ap.add_argument("--qualified-tag", default="expansion:qualified")
    ap.add_argument("--backup-tag", default="expansion:backup")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (l.id, s.raw_data->>'company_linkedin_url')
                       s.raw_data->>'company_linkedin_url' AS company_url,
                       s.raw_data->>'seed_company'         AS company,
                       l.id, l.name, l.current_title, s.raw_data->>'rank' AS rank
                FROM lead_sources s
                JOIN leads l ON l.id = s.lead_id
                JOIN lead_tags t ON t.lead_id = l.id AND t.tag = %s
                WHERE s.source_type = %s
                ORDER BY l.id, s.raw_data->>'company_linkedin_url'
            """, (args.active_tag, args.source_type))
            rows = cur.fetchall()

            by_company: dict[str, list] = defaultdict(list)
            names: dict[str, str] = {}
            for company_url, company, lead_id, name, title, rank in rows:
                by_company[company_url].append((lead_id, name, title, rank))
                names[company_url] = company

            qualified, backup = [], []
            over_cap = []
            for curl, people in by_company.items():
                people.sort(key=sort_key)
                # A person can surface at two companies; first assignment wins so
                # nobody ends up both qualified and backup.
                qualified += [(p[0], curl) for p in people[:args.cap]]
                if len(people) > args.cap:
                    backup += [(p[0], curl) for p in people[args.cap:]]
                    over_cap.append((names[curl], len(people)))

            q_ids = {lid for lid, _ in qualified}
            backup = [(lid, c) for lid, c in backup if lid not in q_ids]
            b_ids = {lid for lid, _ in backup}

            print(f"companies: {len(by_company)}  candidates: {len(rows)}")
            print(f"  at or under cap ({args.cap}): "
                  f"{sum(1 for p in by_company.values() if len(p) <= args.cap)} companies")
            print(f"  over cap:                  {len(over_cap)} companies")
            print(f"\n  -> qualified: {len(q_ids)}")
            print(f"  -> backup   : {len(b_ids)}")
            if over_cap:
                print("\n  biggest benches (company, found -> 5 qualified + backup):")
                for co, n in sorted(over_cap, key=lambda x: -x[1])[:12]:
                    print(f"      {co:<34} {n:>4} -> {args.cap} + {n - args.cap}")

            if not args.dry_run:
                # Recompute from scratch so re-runs with a different --cap are clean.
                cur.execute("DELETE FROM lead_tags WHERE tag IN (%s, %s)",
                            (args.qualified_tag, args.backup_tag))
                cur.executemany("""INSERT INTO lead_tags (lead_id, tag, notes, tagged_by)
                                   VALUES (%s,%s,%s,'tier_expansion_candidates')
                                   ON CONFLICT (lead_id, tag) DO NOTHING""",
                                [(lid, args.qualified_tag, f"top {args.cap} at {c}")
                                 for lid, c in qualified])
                cur.executemany("""INSERT INTO lead_tags (lead_id, tag, notes, tagged_by)
                                   VALUES (%s,%s,%s,'tier_expansion_candidates')
                                   ON CONFLICT (lead_id, tag) DO NOTHING""",
                                [(lid, args.backup_tag, f"beyond top {args.cap} at {c}")
                                 for lid, c in backup])
                conn.commit()
                print("\nwritten.")
            else:
                print("\n--dry-run: nothing written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
