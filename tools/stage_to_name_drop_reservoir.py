#!/usr/bin/env python3
"""marketbase-stage-to-name-drop-reservoir

Stage referral-discovered candidates into a sender-scoped
`referralDiscovery_*_nameDropReservoir_*` campaign, with the per-member
ice breaker frozen into `campaign_members.notes`.

The skill is a thin, opinionated wrapper around `stage_to_campaign.py` that:

  1. Looks up the referrer's lead row (must exist in MarketBase).
  2. Auto-detects the sender by walking the referrer's most recent
     `lead_conversations` and pulling the operator's display name.
  3. Builds the canonical sender-scoped campaign name.
  4. Validates the notes block — every required field must be set.
  5. Defaults `not_before` to today + 7 days.
  6. Derives `opener_variant` from `referrer_seniority`.
  7. Upserts the campaign + members, applies `plan:name_drop_reservoir`,
     and removes any other `plan:*` tag (one plan at a time).

See `~/.claude/skills/acme-propose-reply/SKILL.md` → "The ice breaker"
for the notes block schema, and CONVENTIONS.md for the
`plan:name_drop_reservoir` tag definition.

Usage:
  python3 stage_to_name_drop_reservoir.py \\
    --client Acme \\
    --referrer-url https://www.linkedin.com/in/<...> \\
    --referrer-role "Principal Cloud Security Architect" \\
    --referrer-team "Cloud Security Architecture" \\
    --referrer-seniority senior_leader \\
    --context "CVE patching team — pending Daniel's team confirmation" \\
    --candidates /tmp/candidates.csv          # required cols: linkedin_url, ice_breaker
    [--not-before 2026-06-12]                 # default: today + 7 days
    [--persona qualifiedCyberLeadersV1]       # default: qualifiedCyberLeadersV1
    [--period 202606]                         # default: current YYYYMM
    [--dry-run]
"""
from __future__ import annoacmens

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, normalize_linkedin_url
from stage_to_campaign import ensure_campaign, validate_campaign_name


SENIORITY_TO_VARIANT = {
    "senior_leader": "title",
    "team_member": "team",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--client", required=True)
    ap.add_argument("--referrer-url", required=True,
                    help="LinkedIn URL of the person who pointed us at the candidates.")
    ap.add_argument("--referrer-role", required=True,
                    help="Shortened title the way a human would say it, e.g. "
                         "'Principal Cloud Security Architect'.")
    ap.add_argument("--referrer-team", required=True,
                    help="The referrer's team / function, e.g. 'Cloud Security Architecture'.")
    ap.add_argument("--referrer-seniority", required=True,
                    choices=["senior_leader", "team_member"],
                    help="senior_leader = C-suite/VP/Senior Director/Director/Head of/Principal IC. "
                         "team_member = everyone below. Maps to opener_variant.")
    ap.add_argument("--context", required=True,
                    help="One-line reason — what the referrer pointed us at, paraphrased. "
                         "Used as the seed for each candidate's bridge sentence at compose time.")
    ap.add_argument("--candidates", required=True,
                    help="CSV with required columns: linkedin_url, ice_breaker. "
                         "Each candidate's ice_breaker must be pre-composed.")
    ap.add_argument("--not-before",
                    help="YYYY-MM-DD — graduation date. Default: today + 7 days.")
    ap.add_argument("--persona", default="qualifiedCyberLeadersV1",
                    help="Persona token for campaign name (default: qualifiedCyberLeadersV1)")
    ap.add_argument("--period",
                    help="YYYYMM for campaign name. Default: current month.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would happen, do not write to the DB.")
    args = ap.parse_args()

    today = date.today()
    not_before = args.not_before or (today + timedelta(days=7)).isoformat()
    period_yyyymm = args.period or today.strftime("%Y%m")
    opener_variant = SENIORITY_TO_VARIANT[args.referrer_seniority]

    # Read candidates CSV. Required columns: linkedin_url + ice_breaker.
    candidates: list[dict] = []
    with open(args.candidates, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader, 2):
            url = (row.get("linkedin_url") or "").strip()
            ib = (row.get("ice_breaker") or "").strip()
            if not url:
                sys.exit(f"Row {row_idx}: missing linkedin_url.")
            if not ib:
                sys.exit(f"Row {row_idx} ({url}): missing ice_breaker. "
                         f"The ice breaker MUST be pre-composed at staging time "
                         f"(see acme-propose-reply → 'The ice breaker').")
            candidates.append({
                "url": normalize_linkedin_url(url),
                "ice_breaker": ib,
            })

    if not candidates:
        sys.exit("No candidates in CSV.")

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            # 1. Look up referrer
            cur.execute("""
                SELECT id, name, current_company
                FROM leads WHERE linkedin_url = %s
            """, (normalize_linkedin_url(args.referrer_url),))
            ref = cur.fetchone()
            if not ref:
                sys.exit(f"Referrer not found in MarketBase: {args.referrer_url}\n"
                         f"  Ingest the referrer first.")
            ref_id, ref_name, ref_company = ref
            if not ref_company:
                sys.exit(f"Referrer {ref_name!r} has no current_company in MarketBase. "
                         f"Backfill before staging — the company appears in the "
                         f"ice breaker.")

            # 2. Auto-detect sender from referrer's lead_conversations
            cur.execute("""
                SELECT oo.display_name
                FROM lead_conversations lc
                JOIN outbound_operators oo ON oo.id = lc.operator_id
                WHERE lc.lead_id = %s
                ORDER BY lc.last_message_at DESC NULLS LAST
                LIMIT 1
            """, (ref_id,))
            sender_row = cur.fetchone()
            if not sender_row:
                sys.exit(f"No outbound conversation found between any operator and "
                         f"the referrer {ref_name!r}. Can't determine which sender "
                         f"to use in the campaign name (the 'had a brief chat with X' "
                         f"line must be truthful).")
            sender_display = sender_row[0]
            # Derive sender handle: first name lowercased
            sender_token = sender_display.split()[0].lower()

            # 3. Build campaign name
            campaign_name = (
                f"referralDiscovery_{args.persona}_"
                f"{sender_token}Linkedin_nameDropReservoir_{period_yyyymm}"
            )
            ok, msg = validate_campaign_name(campaign_name)
            if not ok:
                sys.exit(f"Auto-built campaign name failed validation: {campaign_name}\n{msg}")

            # 4. Look up each candidate's lead_id + name (must exist in MarketBase)
            missing = []
            for c in candidates:
                cur.execute("SELECT id, name FROM leads WHERE linkedin_url=%s",
                            (c["url"],))
                row = cur.fetchone()
                if not row:
                    missing.append(c["url"])
                    continue
                c["lead_id"], c["name"] = row
                c["notes"] = (
                    f"referrer: {ref_name}\n"
                    f"referrer_role: {args.referrer_role}\n"
                    f"referrer_company: {ref_company}\n"
                    f"referrer_team: {args.referrer_team}\n"
                    f"referrer_seniority: {args.referrer_seniority}\n"
                    f"not_before: {not_before}\n"
                    f"context: {args.context}\n"
                    f"opener_variant: {opener_variant}\n"
                    f"ice_breaker: {c['ice_breaker']}"
                )

            if missing:
                sys.exit(f"{len(missing)} candidate URL(s) not found in MarketBase — "
                         f"ingest them first with marketbase-upload-leads:\n  "
                         + "\n  ".join(missing))

            # 5. Print plan
            print(f"\nReferrer:  {ref_name} ({ref_company})")
            print(f"Sender:    {sender_display}  (auto-detected from referrer's thread)")
            print(f"Campaign:  {campaign_name}")
            print(f"not_before:    {not_before}")
            print(f"opener_variant: {opener_variant}  (from referrer_seniority={args.referrer_seniority})")
            print(f"\nCandidates ({len(candidates)}):")
            for c in candidates:
                preview = c["ice_breaker"][:100] + ("..." if len(c["ice_breaker"]) > 100 else "")
                print(f"  • {c['name']}")
                print(f"      {preview}")

            if args.dry_run:
                print("\n--dry-run: no DB writes.")
                return 0

            # 6. Upsert campaign
            campaign_id = ensure_campaign(
                cur, campaign_name,
                channel="linkedin_dm",
                persona=args.persona,
                period=period_yyyymm,
                description=(
                    "Sender-scoped reservoir for referral-discovered cyber leaders. "
                    "Members graduate to name-drop cold opener (composed at stage time, "
                    "frozen in notes.ice_breaker) when referrer confirms or not_before passes. "
                    "See plan:name_drop_reservoir tag."
                ),
            )

            # 7. Upsert campaign_members with per-member notes
            for c in candidates:
                cur.execute("""
                    INSERT INTO campaign_members
                        (lead_id, campaign_id, status, last_status_source, notes)
                    VALUES (%s, %s, 'staged', 'marketbase-stage-to-name-drop-reservoir', %s)
                    ON CONFLICT (lead_id, campaign_id) DO UPDATE
                      SET notes = EXCLUDED.notes,
                          last_status_at = now(),
                          last_status_source = EXCLUDED.last_status_source
                """, (c["lead_id"], campaign_id, c["notes"]))

            # 8. Apply plan:name_drop_reservoir; remove any other plan:* tag
            for c in candidates:
                cur.execute("""
                    DELETE FROM lead_tags
                    WHERE lead_id = %s
                      AND tag LIKE 'plan:%%'
                      AND tag <> 'plan:name_drop_reservoir'
                """, (c["lead_id"],))
                cur.execute("""
                    INSERT INTO lead_tags (lead_id, tag, tagged_by)
                    VALUES (%s, 'plan:name_drop_reservoir', 'marketbase-stage-to-name-drop-reservoir')
                    ON CONFLICT (lead_id, tag) DO NOTHING
                """, (c["lead_id"],))

        conn.commit()

    print(f"\n✓ Staged {len(candidates)} candidate(s) to {campaign_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
