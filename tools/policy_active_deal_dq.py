#!/usr/bin/env python3
"""marketbase-policy@active_deal_dq_v1.0

Disqualifies any lead whose CURRENT employer has a live deal in the CRM —
i.e. the company has an open deal, is a won customer, or (per the deletion
policy) had a protecting deal that was deleted in HubSpot and is awaiting
human review. This is how the HubSpot deal-state sync (migration 030,
`company_deals`) actually keeps outreach off companies we're already working.

Mechanism — identical to policy_employer_dq.py: we write a disqualifying
`lead_qualifications` row (qualified=false). That single write then flows
through the existing machinery automatically:
  • staging skips them      — stage-to-campaign / v_qualified_no_campaign
                              only act on qualified=true leads.
  • in-flight ones are pulled — v_pending_removals surfaces active-status
                              leads whose latest qualification is false.

Source of match truth is the view v_leads_at_deal_company (migration 030),
which already resolves lead↔deal two ways (resolved company_id → company URL,
and the deal's match_slug → slug parsed from the lead's company URL). We do
not re-implement matching here.

Writes one lead_qualifications row per matched lead:
  qualifier_name     = 'marketbase-policy'
  qualifier_version  = 'active_deal_dq_v1.0'
  qualified          = false
  persona            = null
  reason             = 'company_is_customer'   (won deal present)
                     | 'company_in_open_deal'  (open deal, not won)
  full_result        = {policy, is_customer, has_open_deal, has_deleted_deal,
                        open_stage, deal_ids, deal_companies}

Idempotent — skips leads whose MOST RECENT qualification row is already an
identical (qualifier_name, qualifier_version, reason).

NOTE on release: like policy_employer_dq, a DQ row persists until the lead is
re-qualified. When a deal closes-LOST it drops out of v_leads_at_deal_company,
but the lead stays DQ'd until its real qualifier is re-run — re-qualification
is the existing path for restoring contactability, not this policy's job.

Usage:
  python3 policy_active_deal_dq.py --client Acme           # apply
  python3 policy_active_deal_dq.py --client Acme --dry-run # preview only
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect  # noqa: E402


QUALIFIER_NAME = "marketbase-policy"
QUALIFIER_VERSION = "active_deal_dq_v1.0"


def find_matches(cur) -> list[dict]:
    """One row per lead currently at a company with a live/won (or
    deleted-but-protecting) deal. Reads the canonical match view."""
    cur.execute(
        """
        SELECT lead_id, linkedin_url, name,
               is_customer, has_open_deal, has_deleted_deal,
               open_stage, deal_ids, deal_companies
        FROM v_leads_at_deal_company
        ORDER BY is_customer DESC, name
        """
    )
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def latest_qualification(cur, lead_id) -> tuple[str, str, str] | None:
    cur.execute(
        """
        SELECT qualifier_name, qualifier_version, reason
        FROM lead_qualifications
        WHERE lead_id = %s
        ORDER BY qualified_at DESC
        LIMIT 1
        """,
        (lead_id,),
    )
    r = cur.fetchone()
    return r if r else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print matches without writing lead_qualifications rows.")
    args = ap.parse_args()

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            matches = find_matches(cur)

        if not matches:
            print("No leads are employed at a company with a live deal.")
            return 0

        breakdown = Counter(
            "company_is_customer" if m["is_customer"] else "company_in_open_deal"
            for m in matches
        )
        print(f"Found {len(matches)} leads to DQ (employed at a deal company):")
        for reason, n in breakdown.most_common():
            print(f"  {reason:<24} {n}")
        n_deleted = sum(1 for m in matches if m["has_deleted_deal"])
        if n_deleted:
            print(f"  (of which {n_deleted} are protected via a DELETED deal "
                  f"pending review — see v_deleted_deals_review)")

        if args.dry_run:
            print("\n[dry-run] — no rows written.")
            return 0

        new_rows = 0
        skipped_already = 0
        with conn.cursor() as cur:
            for m in matches:
                reason = "company_is_customer" if m["is_customer"] else "company_in_open_deal"
                latest = latest_qualification(cur, m["lead_id"])
                if latest is not None and latest == (QUALIFIER_NAME, QUALIFIER_VERSION, reason):
                    skipped_already += 1
                    continue
                full = {
                    "policy": "active_deal_dq",
                    "is_customer": m["is_customer"],
                    "has_open_deal": m["has_open_deal"],
                    "has_deleted_deal": m["has_deleted_deal"],
                    "open_stage": m["open_stage"],
                    "deal_ids": m["deal_ids"],
                    "deal_companies": m["deal_companies"],
                }
                cur.execute(
                    """
                    INSERT INTO lead_qualifications
                        (lead_id, qualifier_name, qualifier_version,
                         qualified, persona, reason, full_result)
                    VALUES (%s, %s, %s, false, NULL, %s, %s)
                    """,
                    (m["lead_id"], QUALIFIER_NAME, QUALIFIER_VERSION,
                     reason, Jsonb(full)),
                )
                new_rows += 1
            conn.commit()

        print(f"\nWrote {new_rows} new DQ rows. Skipped {skipped_already} "
              f"leads whose latest qualification was already the same.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
