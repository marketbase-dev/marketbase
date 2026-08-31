#!/usr/bin/env python3
"""backfill_audience_tags — (re)derive the two competitor AUDIENCE tags.

Audiences are derived labels (see competitor_targeting/audiences.py), so a
policy change is a re-tag, not a re-scrape. Run this after changing any rule in
`titles.py::_EXCLUSION_RULES` or `audiences.py`.

Writes per lead:
  competitor_activity_source     — public-activity mining set (broad)
  competitor_connection_target   — Buyer Monitor connection-tracking set (narrow)
  audience_block:<reason>        — why a person is NOT a connection target

Idempotent: recomputes from scratch each run, removing tags that no longer
apply, so it can't drift from the rules.

  python3 backfill_audience_tags.py --client "Amplifier Security" [--dry-run]
"""
from __future__ import annoacmens
import argparse, os, sys
from pathlib import Path
sys.path.insert(0, os.path.expanduser("~/.claude/tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from competitor_targeting.audiences import (  # noqa: E402
    audience_verdict, ACTIVITY_MINING, CONNECTION_TRACKING)
from lib import connect  # noqa: E402

TAGGER = "backfill_audience_tags"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with connect(args.client) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT l.id, l.current_title,
                   ls.raw_data->>'role_class', ls.raw_data->>'verdict',
                   ls.raw_data->>'founder_bucket', ls.raw_data->>'tier_one',
                   EXISTS (SELECT 1 FROM lead_tags t
                           WHERE t.lead_id = l.id AND t.tag = 'left_competitor')
            FROM leads l
            JOIN lead_sources ls ON ls.lead_id = l.id
                                AND ls.source_type = 'find_competitor_salesperson'
        """)
        # A lead can have several source rows (sourced under >1 competitor).
        # Keep the most permissive verdict per lead: if ANY source run says
        # connection-target, they are one.
        best: dict = {}
        for lid, title, rc, verdict, fb, tier, departed in cur.fetchall():
            tier_one = {"true": True, "false": False}.get((tier or "").lower())
            v = audience_verdict(title=title or "", role_class=rc or "",
                                 verdict=verdict or "", departed=departed,
                                 founder_bucket=fb, tier_one=tier_one)
            prev = best.get(lid)
            if prev is None or (CONNECTION_TRACKING in v["audiences"]
                                and CONNECTION_TRACKING not in prev["audiences"]):
                best[lid] = v

        stats = {"activity": 0, "connection": 0, "blocked": {}}
        for lid, v in best.items():
            conn_ok = CONNECTION_TRACKING in v["audiences"]
            stats["activity"] += 1
            stats["connection"] += int(conn_ok)
            if v["connection_block"]:
                b = v["connection_block"]
                stats["blocked"][b] = stats["blocked"].get(b, 0) + 1
            if args.dry_run:
                continue
            cur.execute("DELETE FROM lead_tags WHERE lead_id=%s AND (tag=%s OR tag=%s "
                        "OR tag LIKE 'audience_block:%%')",
                        (lid, ACTIVITY_MINING, CONNECTION_TRACKING))
            tags = [ACTIVITY_MINING] + ([CONNECTION_TRACKING] if conn_ok else [])
            if v["connection_block"]:
                tags.append(f"audience_block:{v['connection_block']}")
            for t in tags:
                cur.execute("INSERT INTO lead_tags (lead_id, tag, tagged_by) "
                            "VALUES (%s,%s,%s) ON CONFLICT (lead_id, tag) DO NOTHING",
                            (lid, t, TAGGER))
        if not args.dry_run:
            conn.commit()

    print(f"[audience-backfill] client={args.client}{' (dry-run)' if args.dry_run else ''}")
    print(f"  {ACTIVITY_MINING:32s} {stats['activity']}")
    print(f"  {CONNECTION_TRACKING:32s} {stats['connection']}")
    print("  blocked from connection-tracking:")
    for k, v in sorted(stats["blocked"].items(), key=lambda x: -x[1]):
        print(f"     {k:34s} {v}")


if __name__ == "__main__":
    main()
