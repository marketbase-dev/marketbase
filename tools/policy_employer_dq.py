#!/usr/bin/env python3
"""marketbase-policy@employer_dq_v2.0

Disqualifies any lead whose current employer carries a disqualifying
company_relationships row. Generalizes the v1 competitor-only policy to also
cover security vendors, known buyers of competitor products, "self"
(employees of the client we're selling for), and customers.

Writes one lead_qualifications row per match:
  qualifier_name     = 'marketbase-policy'
  qualifier_version  = 'employer_dq_v2.0'
  qualified          = false
  persona            = null
  reason             = 'employed_at_<relationship>'
  full_result        = {policy, relationship, scope, company_id, company_name,
                        company_linkedin_url, relationship_notes, matched_by}

Idempotent — skips leads whose MOST RECENT qualification row is already an
identical (qualifier_name, qualifier_version, reason). New competitor flags
or new leads at a previously-flagged company will surface on re-run.

Matching:
  Tries (in order) for each disqualifying company:
    1. leads.current_company_url ILIKE companies.linkedin_url (URL match)
    2. lower(leads.current_company) = lower(companies.name)   (exact name match)

Usage:
  python3 policy_employer_dq.py --client Acme           # apply
  python3 policy_employer_dq.py --client Acme --dry-run # preview only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect  # noqa: E402


QUALIFIER_NAME = "marketbase-policy"
QUALIFIER_VERSION = "employer_dq_v2.0"

# Relationship values that trigger DQ (kept in sync with CONVENTIONS.md).
DQ_RELATIONSHIPS = (
    "competitor",
    "security_vendor",
    "bought_competitor_product",
    "self",
    "customer",
)


def find_matches(cur) -> list[dict]:
    """Return one row per (lead, company_relationship) that should be DQ'd.

    A lead can match more than one disqualifying company (rare); we pick the
    most-restrictive single match using a priority on relationship type.
    """
    cur.execute(
        """
        WITH dq AS (
            SELECT cr.id   AS rel_id,
                   cr.company_id,
                   cr.relationship,
                   cr.scope,
                   cr.notes,
                   c.name           AS company_name,
                   c.linkedin_url   AS company_linkedin_url
            FROM company_relationships cr
            JOIN companies c ON c.id = cr.company_id
            WHERE cr.relationship = ANY(%s)
        ),
        matches AS (
            SELECT l.id AS lead_id, l.name AS lead_name,
                   l.current_company, l.current_company_url,
                   dq.*,
                   CASE
                       WHEN l.current_company_url IS NOT NULL
                        AND dq.company_linkedin_url IS NOT NULL
                        AND lower(l.current_company_url) = lower(dq.company_linkedin_url)
                       THEN 'linkedin_url'
                       WHEN l.current_company IS NOT NULL
                        AND lower(l.current_company) = lower(dq.company_name)
                       THEN 'company_name'
                       ELSE NULL
                   END AS matched_by
            FROM leads l
            JOIN dq ON (
                (l.current_company_url IS NOT NULL
                 AND dq.company_linkedin_url IS NOT NULL
                 AND lower(l.current_company_url) = lower(dq.company_linkedin_url))
              OR
                (l.current_company IS NOT NULL
                 AND lower(l.current_company) = lower(dq.company_name))
            )
        ),
        ranked AS (
            -- One DQ row per lead — pick the highest-priority relationship if
            -- the lead happens to match more than one.
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY lead_id
                ORDER BY array_position(%s::text[], relationship)
            ) AS rn
            FROM matches
        )
        SELECT lead_id, lead_name, current_company, current_company_url,
               company_id, company_name, company_linkedin_url,
               relationship, scope, notes, matched_by
        FROM ranked
        WHERE rn = 1
        ORDER BY relationship, company_name, lead_name
        """,
        (list(DQ_RELATIONSHIPS), list(DQ_RELATIONSHIPS)),
    )
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def latest_qualification(cur, lead_id) -> tuple[str, str, str] | None:
    """Return (qualifier_name, qualifier_version, reason) of the latest row,
    or None if no qualification exists."""
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
            print(f"No leads match any disqualifying company relationship "
                  f"({', '.join(DQ_RELATIONSHIPS)}).")
            return 0

        # Summary by relationship
        from collections import Counter
        breakdown = Counter(m["relationship"] for m in matches)
        print(f"Found {len(matches)} leads to DQ:")
        for rel, n in breakdown.most_common():
            print(f"  {rel:<28} {n}")

        if args.dry_run:
            print("\n[dry-run] — no rows written.")
            return 0

        new_rows = 0
        skipped_already = 0
        with conn.cursor() as cur:
            for m in matches:
                reason = f"employed_at_{m['relationship']}"
                latest = latest_qualification(cur, m["lead_id"])
                if latest is not None and latest == (QUALIFIER_NAME, QUALIFIER_VERSION, reason):
                    skipped_already += 1
                    continue
                full = {
                    "policy": "employer_dq",
                    "relationship": m["relationship"],
                    "scope": m["scope"],
                    "company_id": str(m["company_id"]),
                    "company_name": m["company_name"],
                    "company_linkedin_url": m["company_linkedin_url"],
                    "relationship_notes": m["notes"],
                    "matched_by": m["matched_by"],
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
