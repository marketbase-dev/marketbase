#!/usr/bin/env python3
"""recompute_all_ptl_stats — refresh stat fields for every classified PTL.

For each `potential_thought_leader`-tagged lead that already has a
`demand_gen_signals_enricher` row (any version):
  1. Re-fetch posts via Saleleads /api/v1/user/posts (one fetch per lead).
  2. Recompute the 8 stat fields with the corrected dominant-author
     reshare filter (fixes the issue where Saleleads reshare reactions
     inflated avg_reactions_3mo).
  3. UPSERT a row as (lead_id, demand_gen_signals_enricher, '1.0') — for
     leads already at v1.0 this overwrites stats in place; for legacy-only
     leads (the 850 CSV consultants + 388 backfilled) it writes a NEW v1.0
     row with their existing GPT-derived signals + the fresh stats. The
     legacy row stays for audit trail.

After this, RE-CLASSIFY with demand_gen_persona_classifier@1.0.

Cost: ~$0.005 per lead (one Saleleads call).
"""
from __future__ import annoacmens

import argparse
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, load_client_env
from engagers_research import saleleads_get, _slug_from_url
from demand_gen_prompts import compute_stats


STAT_FIELDS = ["total_posts_fetched", "original_posts_fetched",
               "posts_3mo", "avg_reactions_3mo",
               "posts_6mo", "avg_reactions_6mo",
               "posts_12mo", "avg_reactions_12mo"]


def fetch_user_posts(linkedin_url: str, max_pages: int = 3) -> list[dict]:
    slug = _slug_from_url(linkedin_url)
    if not slug: return []
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        d = saleleads_get("/api/v1/user/posts", {"username": slug, "page": page})
        if not d or not d.get("success"): break
        data = d.get("data") or []
        if not data: break
        out.extend(data)
        if len(data) < 20: break
    return out


RUN_TAG = "recompute-all-ptl-stats-2026-05-27"


def _is_dead_conn(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "connection is closed" in msg or "consuming input failed" in msg or "ssl connection has been closed" in msg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", action="store_true",
                    help="Skip leads already written by this run's RUN_TAG.")
    args = ap.parse_args()
    load_client_env(args.client)

    def open_conn():
        return connect(args.client).__enter__()

    conn = open_conn()
    with conn.cursor() as cur:
        # Select every PTL with at least one demand_gen_signals_enricher
        # row. For each, also grab the most-recent payload (any version)
        # so we can merge GPT-derived fields with the fresh stats.
        cur.execute("""
            WITH ptl AS (SELECT DISTINCT lead_id FROM lead_tags WHERE tag='potential_thought_leader'),
                 latest AS (
                   SELECT DISTINCT ON (lead_id) lead_id, payload, enricher_version
                   FROM lead_signals
                   WHERE enricher_name='demand_gen_signals_enricher'
                   ORDER BY lead_id, enriched_at DESC
                 )
            SELECT l.id, l.linkedin_url, l.name, latest.payload, latest.enricher_version
            FROM ptl
            JOIN leads l ON l.id = ptl.lead_id
            JOIN latest ON latest.lead_id = l.id
            ORDER BY l.name
        """)
        targets = [{"id": r[0], "linkedin_url": r[1], "name": r[2],
                    "payload": r[3] or {}, "prior_version": r[4]} for r in cur.fetchall()]

        done_ids: set = set()
        if args.resume:
            cur.execute("""
                SELECT lead_id FROM lead_signals
                WHERE enricher_name='demand_gen_signals_enricher'
                  AND enricher_version='1.0'
                  AND enriched_by=%s
            """, (RUN_TAG,))
            done_ids = {r[0] for r in cur.fetchall()}
            targets = [t for t in targets if t["id"] not in done_ids]

    if args.limit: targets = targets[:args.limit]
    print(f"Targets: {len(targets)} PTL(s)"
          + (f" (skipping {len(done_ids)} already done)" if done_ids else ""))
    if args.dry_run: print("(dry-run — no writes)")

    n_updated = n_inserted = n_skipped = n_errors = 0
    big_drops: list = []
    for i, t in enumerate(targets, 1):
        try:
            posts = fetch_user_posts(t["linkedin_url"])
            if not posts:
                n_skipped += 1
                print(f"[{i}/{len(targets)}] ⊝ skip (no posts): {t['name']}", flush=True)
                continue
            new_stats = compute_stats(posts)
            old_a3 = t["payload"].get("avg_reactions_3mo")
            old_p3 = t["payload"].get("posts_3mo")
            merged = dict(t["payload"])
            for k in STAT_FIELDS:
                merged[k] = new_stats[k]
            if args.dry_run:
                print(f"[{i}/{len(targets)}] (dry) {t['name'][:26]:<26}  "
                      f"posts_3mo: {old_p3}→{merged['posts_3mo']}  "
                      f"avg_rxn: {old_a3}→{merged['avg_reactions_3mo']}", flush=True)
                continue
            # Reconnect-on-dead-conn around the UPSERT
            for attempt in range(3):
                try:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO lead_signals
                              (lead_id, enricher_name, enricher_version, payload,
                               enriched_at, enriched_by)
                            VALUES (%s, 'demand_gen_signals_enricher', '1.0', %s, now(), %s)
                            ON CONFLICT (lead_id, enricher_name, enricher_version) DO UPDATE
                              SET payload = EXCLUDED.payload,
                                  enriched_at = now(),
                                  enriched_by = EXCLUDED.enriched_by
                            RETURNING (xmax = 0) AS inserted
                        """, (t["id"], Jsonb(merged), RUN_TAG))
                        was_insert = cur.fetchone()[0]
                    conn.commit()
                    break
                except Exception as db_e:
                    if _is_dead_conn(db_e) and attempt < 2:
                        print(f"    ⟳ DB connection died, reopening (attempt {attempt+1})", flush=True)
                        try: conn.close()
                        except Exception: pass
                        conn = open_conn()
                        continue
                    raise
            if was_insert: n_inserted += 1
            else: n_updated += 1
            drop = (old_a3 or 0) - (merged["avg_reactions_3mo"] or 0)
            if drop > 50:
                big_drops.append((t["name"], old_a3, merged["avg_reactions_3mo"]))
            print(f"[{i}/{len(targets)}] ✓ {t['name'][:26]:<26}  "
                  f"avg_rxn: {old_a3}→{merged['avg_reactions_3mo']}  "
                  f"posts_3mo: {old_p3}→{merged['posts_3mo']}", flush=True)
        except Exception as e:
            n_errors += 1
            print(f"[{i}/{len(targets)}] ✗ {t['name']}: {e}", flush=True)

    print(f"\nSummary: inserted_v1={n_inserted}  updated_v1={n_updated}  "
          f"skipped(no-posts)={n_skipped}  errors={n_errors}")
    if big_drops:
        print(f"\nTop 15 biggest avg-reaction drops (legacy was over-counted):")
        big_drops.sort(key=lambda x: (x[1] or 0) - (x[2] or 0), reverse=True)
        for n, oa, na in big_drops[:15]:
            print(f"  {n[:30]:<30}  avg_rxn: {oa}→{na}")
    try: conn.close()
    except Exception: pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
