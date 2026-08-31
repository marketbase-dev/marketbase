#!/usr/bin/env python3
"""engager_pool_fetcher@1.0

For each `potential_thought_leader`-tagged lead, fetch their recent
LinkedIn posts via Saleleads /api/v1/user/posts, then for each post pull
every reactor and commenter — writing posts + engagements + new engager
leads straight to Postgres.

This is a sibling to marketbase-engagers-research (which targets flagged-
company senior execs); same machinery, different target population.
Reuses the helpers from engagers_research.py — single source of truth
for the fetch + write logic.

On success per candidate: tags `engagers_researched`, removes
`engager_research_queued`. Idempotent — skips candidates recently
researched (recent_search_exists) unless --refresh.

CLI:
  # Single candidate
  python3 engager_pool_fetcher.py --client Acme-AI \\
      --lead-url https://www.linkedin.com/in/somebody/

  # CSV of candidates (URL column auto-detected)
  python3 engager_pool_fetcher.py --client Acme-AI \\
      --lead-file leads.csv [--refresh] [--limit 5]

  # Every potential_thought_leader candidate (the big one)
  python3 engager_pool_fetcher.py --client Acme-AI \\
      --all-tagged-ptl [--refresh] [--limit 50]
"""
from __future__ import annoacmens

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import load_client_env, database_url, normalize_linkedin_url, register_processor_from_yaml
from engagers_research import (
    fetch_user_posts, harvest_posts_for,
    recent_search_exists, insert_search,
    _slug_from_url,
    _fresh_conn, _ensure_conn,
    SaleleadsCreditExhausted,
)


def _is_reshare(post: dict, expected_author_url: str | None = None) -> bool:
    """Detect Saleleads-shape reshare posts. Mirrors the SQL-level filter
    used by downstream reports (acme-ai-carousel-engager-buckets etc.).

    The PRIMARY signal in the actual Saleleads `/user/posts` payload is the
    `author` object: when a leader reshares someone else's post, `author.url`
    points at the *original* author, not the leader. So a post whose author
    slug differs from the leader we asked for is a reshare — its reactors were
    engaging with the original author's content, not this leader's. The older
    key checks (`reshared`, `is_repost`, `type`=='repost', …) are kept as
    belt-and-suspenders but DO NOT appear in the current payload shape, which
    is why every reshare slipped through before this was added."""
    if expected_author_url:
        want = _slug_from_url(expected_author_url)
        got = _slug_from_url((post.get("author") or {}).get("url") or "")
        if want and got and want.lower() != got.lower():
            return True
    if post.get("reshared") is True or post.get("is_repost") is True:
        return True
    if post.get("repost_urn") or post.get("reposted_post") or post.get("shared_post"):
        return True
    if (post.get("type") or "").lower() == "repost":
        return True
    return False
# Note: we deliberately do NOT import the URN→vanity resolvers
# (vanity_from_saleleads_search / vanity_from_urn_url). Saleleads'
# /user/posts and /user/profile accept URNs directly via the `username`
# parameter, so the resolution dance is unnecessary.


FETCHER_NAME = "engager_pool_fetcher"
FETCHER_VERSION = "1.0"


YAML_SPEC = f"""
name: {FETCHER_NAME}
version: "{FETCHER_VERSION}"
processor_type: fetcher

description: >
  For each lead tagged potential_thought_leader (or explicitly listed),
  fetches their recent LinkedIn posts via Saleleads /api/v1/user/posts
  and harvests every reactor + commenter into post_engagements (creating
  new lead rows for previously-unseen engagers).

  Sibling to engagers-research (which targets flagged-company senior
  execs). Same fetch + write machinery; different target population.

inputs:
  fields_consulted:
    - leads.linkedin_url (resolves slug slug for the Saleleads call)
    - lead_tags  (potential_thought_leader, engagers_researched, engager_research_queued)
    - searches (skips re-fetch if a recent user_posts:<slug> search exists)
  external_apis:
    - Saleleads /api/v1/user/posts
    - Saleleads /api/v1/post/reactions
    - Saleleads /api/v1/post/comments
    - LeadMagic /profile-search (fallback for URN-encoded LinkedIn URLs)

outputs:
  writes_to_tables: [posts, post_engagements, leads, lead_sources, lead_tags, searches]
  tags_applied: [engagers_researched]
  tags_removed: [engager_research_queued]
  lead_sources_source_type: linkedin_post_engagement

decision_rule: |
  N/A — fetchers gather data, they don't decide.

  Per-candidate flow:
    1. Skip if a recent (default 14 days) user_posts:<slug> search row
       exists in `searches`, unless --refresh.
    2. Resolve LinkedIn slug slug. URN-encoded URLs are looked up via
       LeadMagic /profile-search; if that fails, skip the candidate.
    3. Saleleads /api/v1/user/posts → up to N posts (default 20).
    4. For each post: upsert into `posts` (with full raw_data so the
       orchestrator's reshare filter can read JSONB markers); then fetch
       reactions + comments and INSERT each engager as a lead +
       post_engagement + lead_source row.
    5. On success: tag candidate `engagers_researched`; remove
       `engager_research_queued`.

rule_changes: |
  1.0 (2026-05-27): initial — split out of engagers-research so the
  flagged-company-execs flow and the ptl-candidates flow can evolve
  independently. Same underlying helpers (saleleads_get, upsert_post,
  upsert_lead_from_engagement, harvest_posts_for); only the target
  selection differs.
"""


# ── Candidate selection ──────────────────────────────────────────────────

def select_candidates(cur, *, lead_url: str | None, lead_file: str | None,
                      all_tagged_ptl: bool) -> list[dict]:
    """Return [{id, linkedin_url, public_id, name}, ...] for each ptl candidate."""
    base = """
        SELECT id, linkedin_url, public_id, name
        FROM leads
    """
    if lead_url:
        cur.execute(base + " WHERE linkedin_url = %s",
                    (normalize_linkedin_url(lead_url),))
    elif lead_file:
        urls = _read_urls_from_file(Path(lead_file))
        if not urls: return []
        cur.execute(base + " WHERE linkedin_url = ANY(%s)", (urls,))
    elif all_tagged_ptl:
        cur.execute(base + """
            WHERE id IN (SELECT lead_id FROM lead_tags
                         WHERE tag = 'potential_thought_leader')
        """)
    else:
        return []
    return [{"id": r[0], "linkedin_url": r[1], "public_id": r[2], "name": r[3]}
            for r in cur.fetchall()]


def _read_urls_from_file(path: Path) -> list[str]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        col = next((c for c in ("linkedin_url","profile_url","url","LinkedIn URL")
                    if c in (reader.fieldnames or [])), None)
        if col is None: sys.exit("No URL column in CSV.")
        return [normalize_linkedin_url(row[col]) for row in reader if row.get(col)]


# ── Tag lifecycle ────────────────────────────────────────────────────────

def _add_tag(cur, lead_id, tag: str, notes: str | None = None):
    cur.execute("""
        INSERT INTO lead_tags (lead_id, tag, notes, tagged_by)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (lead_id, tag) DO UPDATE
          SET notes = EXCLUDED.notes, tagged_at = now()
    """, (lead_id, tag, notes, f"{FETCHER_NAME}@{FETCHER_VERSION}"))


def _remove_tag(cur, lead_id, tag: str):
    cur.execute("DELETE FROM lead_tags WHERE lead_id=%s AND tag=%s", (lead_id, tag))


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch posts + engagers for potential_thought_leader candidates.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--client", required=True)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--lead-url", help="One LinkedIn URL.")
    target.add_argument("--lead-file", help="CSV with a URL column.")
    target.add_argument("--all-tagged-ptl", action="store_true",
                        help="All leads tagged potential_thought_leader.")
    ap.add_argument("--max-posts-per-candidate", type=int, default=50,
                    help="Cap ORIGINAL posts kept per candidate (default 50). "
                         "Reshares are filtered out before counting against this cap, "
                         "so we may fetch more than this many raw posts from Saleleads "
                         "to net the target number of originals.")
    ap.add_argument("--max-engagers-per-post", type=int, default=500)
    ap.add_argument("--no-reactors", action="store_true")
    ap.add_argument("--no-commenters", action="store_true")
    ap.add_argument("--max-age-days", type=int, default=14,
                    help="Skip candidates whose user_posts search is more recent than this (unless --refresh).")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-fetch even if a recent search exists.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap number of candidates processed per run (0=unlimited).")
    args = ap.parse_args()

    load_client_env(args.client)
    db_url = database_url(args.client)

    # Self-register
    try:
        _, _, action = register_processor_from_yaml(
            args.client, YAML_SPEC, created_by=f"{FETCHER_NAME}-{FETCHER_VERSION}")
        if action == "inserted":
            print(f"✓ registered processor {FETCHER_NAME}@{FETCHER_VERSION}")
    except Exception as e:
        print(f"⚠ self-registration failed (continuing): {e}", file=sys.stderr)

    # Discover candidates
    with _fresh_conn(db_url) as conn:
        with conn.cursor() as cur:
            candidates = select_candidates(
                cur, lead_url=args.lead_url, lead_file=args.lead_file,
                all_tagged_ptl=args.all_tagged_ptl)
    if not candidates:
        print("No candidates.")
        return 0
    if args.limit:
        candidates = candidates[:args.limit]
    print(f"=== {args.client}: fetching engagers for {len(candidates)} ptl candidate(s) ===")

    grand = {"posts": 0, "reactions": 0, "comments": 0}
    aborted = False
    # Track consecutive leaders that returned 0 posts. If too many in a row,
    # something is broken upstream (Saleleads silently returning empty data
    # while still consuming our plan quota). Abort like credit-exhausted.
    consecutive_zero_posts = 0
    MAX_CONSECUTIVE_ZERO_POSTS = int(os.environ.get("ENGAGER_MAX_CONSECUTIVE_ZERO", "5"))
    for i, cand in enumerate(candidates, 1):
        name = cand["name"] or cand["linkedin_url"]
        print(f"\n[{i}/{len(candidates)}] {name}  ({cand['linkedin_url']})")

        # Resolve identifier for Saleleads /user/posts.
        # Saleleads' `username` param is polymorphic — it accepts both
        # slug slugs (e.g. ofer-miller) AND URN-encoded forms (e.g.
        # ACwAAApPi4EBlRT4TVtWIiCkrSDZe3dq5PQ2_8w). Pass whatever's in
        # the URL as-is; no resolution dance needed.
        slug = _slug_from_url(cand["linkedin_url"]) or cand["public_id"] or ""
        if not slug:
            print(f"  ! skip: no slug extractable from URL")
            continue

        search_key = f"user_posts:{slug}"
        conn = _fresh_conn(db_url)
        try:
            with conn.cursor() as cur:
                if not args.refresh and recent_search_exists(cur, search_key, args.max_age_days):
                    print(f"  · skipped (recent search for {search_key!r})")
                    continue

            print(f"  → fetching /user/posts?username={slug}")
            try:
                raw_posts = fetch_user_posts(slug)
                if not raw_posts:
                    consecutive_zero_posts += 1
                    print(f"  · 0 posts returned (consecutive zero #{consecutive_zero_posts}; "
                          f"not recorded as search — will retry on next run)")
                    if consecutive_zero_posts > MAX_CONSECUTIVE_ZERO_POSTS:
                        print(f"  ✖ ABORTING RUN: {consecutive_zero_posts} consecutive leaders "
                              f"returned 0 posts. Saleleads is likely degraded "
                              f"(silently returning empty while consuming plan quota).",
                              file=sys.stderr)
                        print(f"    Remaining candidates ({len(candidates)-i}) NOT processed.",
                              file=sys.stderr)
                        aborted = True
                        break
                    continue
                # Got real data — reset the consecutive-zero counter.
                consecutive_zero_posts = 0
                # Keep only ORIGINAL posts before capping at --max-posts-per-candidate.
                # Reshares don't belong in this leader's engager pool (those reactors
                # were engaging with the original author's content, not this leader).
                originals = [p for p in raw_posts
                             if not _is_reshare(p, cand["linkedin_url"])]
                reshares = len(raw_posts) - len(originals)
                posts = originals[:args.max_posts_per_candidate]
                print(f"    raw={len(raw_posts)}  originals={len(originals)}  reshares={reshares}  kept={len(posts)}")

                # Source label that ties engagers back to the candidate
                source_label = f"engager of ptl candidate: {name}"
                conn = _ensure_conn(conn, db_url)
                stats = harvest_posts_for(
                    db_url, "ptl_candidate", posts,
                    cand["name"] or "", cand["linkedin_url"] or "",
                    source_label, args.max_engagers_per_post,
                    include_reactors=not args.no_reactors,
                    include_commenters=not args.no_commenters,
                )
            except SaleleadsCreditExhausted as e:
                print(f"  ✖ ABORTING RUN: {e}", file=sys.stderr)
                print(f"    Remaining candidates ({len(candidates)-i+1}) NOT processed. "
                      f"Resume with same --lead-file (+ --refresh if needed) once credits restored.",
                      file=sys.stderr)
                aborted = True
                break
            for k in grand: grand[k] += stats[k]

            conn = _ensure_conn(conn, db_url)
            with conn.cursor() as cur:
                insert_search(cur, search_key,
                              {"candidate": cand["name"], "slug": slug,
                               "max_posts": args.max_posts_per_candidate,
                               "max_engagers": args.max_engagers_per_post},
                              stats["posts"])
                _add_tag(cur, cand["id"], "engagers_researched",
                         f"posts={stats['posts']} reactions={stats['reactions']} comments={stats['comments']}")
                _remove_tag(cur, cand["id"], "engager_research_queued")
            conn.commit()
            print(f"  ✓ {stats['posts']} posts, {stats['reactions']} reactions, {stats['comments']} comments  →  tagged engagers_researched")
        finally:
            try: conn.close()
            except: pass

    suffix = " (ABORTED — Saleleads degraded)" if aborted else ""
    print(f"\nDONE{suffix} — totals across {len(candidates)} candidate(s): "
          f"{grand['posts']} posts  {grand['reactions']} reactions  {grand['comments']} comments")
    # Per-process Saleleads cost snapshot (advisory)
    try:
        from engagers_research import saleleads_cost_snapshot
        s = saleleads_cost_snapshot()
        print(f"  saleleads: calls={s['calls_total']}  success={s['calls_success']}  "
              f"charged_denials={s['calls_charged_denial']}  "
              f"cost_units_charged={s['cost_charged']}  cost_units_free={s['cost_free']}")
    except Exception:
        pass
    # Non-zero exit on abort so wrapper scripts (the per-leader loop) know to stop too.
    return 2 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
