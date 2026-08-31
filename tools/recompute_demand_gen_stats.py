#!/usr/bin/env python3
"""recompute_demand_gen_stats — fix posting-stat fields in lead_signals
for leads whose signals came from the Python (Saleleads) enricher.

The legacy Node.js ETL used fresh-linkedin-profile-data, which exposed
reshare-marker fields (reshared/repost_urn/...) — its stats are correct.
The Saleleads /api/v1/user/posts endpoint doesn't surface those fields
AND mixes reshares of OTHERS' content into the user's feed. The original
demand_gen_signals_enricher@1.0 was vulnerable: posts_3mo and
avg_reactions_3mo got inflated by reshares.

This tool re-fetches each affected lead's posts and re-computes ONLY the
stat fields (posts_3mo, avg_reactions_3mo, etc.) using the corrected
compute_stats which now filters by dominant author URN. GPT prompts are
NOT re-run (their outputs depend on profile + posts content, which the
reshare fix doesn't materially change — and we'd rather spend $1 than $11).

After running this, RE-CLASSIFY with demand_gen_persona_classifier@1.0
to update qualification verdicts.

CLI:
  # Default: affect only signals written by the buggy Python enricher
  python3 recompute_demand_gen_stats.py --client Acme-AI

  # Explicit lead set
  python3 recompute_demand_gen_stats.py --client Acme-AI --lead-file urls.csv

  # Dry-run: show before/after diffs, don't write
  python3 recompute_demand_gen_stats.py --client Acme-AI --dry-run --limit 10
"""
from __future__ import annoacmens

import argparse
import csv
import json
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, load_client_env, normalize_linkedin_url
from engagers_research import saleleads_get, _slug_from_url
from demand_gen_prompts import compute_stats


STAT_FIELDS = ["total_posts_fetched", "original_posts_fetched",
               "posts_3mo", "avg_reactions_3mo",
               "posts_6mo", "avg_reactions_6mo",
               "posts_12mo", "avg_reactions_12mo"]


def fetch_user_posts(linkedin_url: str, max_pages: int = 3) -> list[dict]:
    slug = _slug_from_url(linkedin_url)
    if not slug:
        return []
    all_posts: list[dict] = []
    for page in range(1, max_pages + 1):
        d = saleleads_get("/api/v1/user/posts", {"username": slug, "page": page})
        if not d or not d.get("success"): break
        data = d.get("data") or []
        if not data: break
        all_posts.extend(data)
        if len(data) < 20: break
    return all_posts


def select_targets(cur, *, lead_url: str | None, lead_file: str | None,
                   enriched_by_filter: str | None) -> list[dict]:
    base = """
        SELECT l.id, l.linkedin_url, l.name, ls.id AS signal_id, ls.payload, ls.enriched_by
        FROM leads l
        JOIN lead_signals ls ON ls.lead_id = l.id
        WHERE ls.enricher_name='demand_gen_signals_enricher'
          AND ls.enricher_version='1.0'
    """
    params: list = []
    if lead_url:
        base += " AND l.linkedin_url = %s"
        params.append(normalize_linkedin_url(lead_url))
    elif lead_file:
        with open(lead_file, newline="") as f:
            reader = csv.DictReader(f)
            col = next((c for c in ("linkedin_url","profile_url","url","LinkedIn URL")
                        if c in (reader.fieldnames or [])), None)
            if col is None: sys.exit("No URL column in CSV.")
            urls = [normalize_linkedin_url(row[col]) for row in reader if row.get(col)]
        base += " AND l.linkedin_url = ANY(%s)"
        params.append(urls)
    elif enriched_by_filter:
        base += " AND ls.enriched_by = %s"
        params.append(enriched_by_filter)
    cur.execute(base, tuple(params))
    cols = ["id", "linkedin_url", "name", "signal_id", "payload", "enriched_by"]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-compute Saleleads-era stats with reshare fix.")
    ap.add_argument("--client", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--lead-url")
    g.add_argument("--lead-file")
    g.add_argument("--enriched-by", default="demand-gen-signals-enricher-1.0",
                   help="Restrict to signals with this enriched_by tag (default: Python enricher only).")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    load_client_env(args.client)

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            targets = select_targets(
                cur, lead_url=args.lead_url, lead_file=args.lead_file,
                enriched_by_filter=None if (args.lead_url or args.lead_file) else args.enriched_by)
        if args.limit: targets = targets[:args.limit]
        print(f"Targets: {len(targets)} lead(s) — re-fetching posts + recomputing stats")
        if args.dry_run: print("(dry-run — no writes)")

        n_updated = n_skipped = n_errors = 0
        flips = []
        for i, t in enumerate(targets, 1):
            try:
                posts = fetch_user_posts(t["linkedin_url"])
                if not posts:
                    n_skipped += 1
                    print(f"[{i}/{len(targets)}] ⊝ skip (no posts): {t['name']}")
                    continue
                new_stats = compute_stats(posts)
                old_payload = t["payload"] or {}
                old_p3 = old_payload.get("posts_3mo")
                old_a3 = old_payload.get("avg_reactions_3mo")
                new_p3 = new_stats["posts_3mo"]
                new_a3 = new_stats["avg_reactions_3mo"]
                # Build the updated payload — only overwrite stat fields
                merged = dict(old_payload)
                for k in STAT_FIELDS:
                    merged[k] = new_stats[k]
                diff_str = f"posts_3mo: {old_p3}→{new_p3}  avg_rxn_3mo: {old_a3}→{new_a3}"
                if old_p3 != new_p3 or old_a3 != new_a3:
                    flips.append((t["name"], old_p3, new_p3, old_a3, new_a3))
                if args.dry_run:
                    print(f"[{i}/{len(targets)}] (dry) {t['name']:<26} {diff_str}")
                else:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE lead_signals SET payload = %s, enriched_at = now()
                            WHERE id = %s
                        """, (Jsonb(merged), t["signal_id"]))
                    conn.commit()
                    print(f"[{i}/{len(targets)}] ✓ {t['name']:<26} {diff_str}")
                n_updated += 1
            except Exception as e:
                n_errors += 1
                print(f"[{i}/{len(targets)}] ✗ {t['name']}: {e}")

        print(f"\nSummary: recomputed={n_updated}  skipped(no-posts)={n_skipped}  errors={n_errors}")
        if flips:
            print(f"\nTop 10 biggest avg-reaction drops:")
            flips.sort(key=lambda x: (x[3] or 0) - (x[4] or 0), reverse=True)
            for name, op, np_, oa, na in flips[:10]:
                if (oa or 0) > (na or 0):
                    print(f"  {name:<26}  avg_rxn: {oa}→{na}  (posts_3mo: {op}→{np_})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
