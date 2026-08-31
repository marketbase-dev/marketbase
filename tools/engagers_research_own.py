#!/usr/bin/env python3
"""Walk Acme's OWN posts (founders + company page) deeply: all posts ever,
all reactors + commenters per post, all written into MarketBase.

Why a separate script (vs `engagers_research.py --company acme`):
  - We need *every* post since the founder joined, not the 20 most recent.
  - The `Acme (self)` find_senior_execs ingest contains misclassified
    leads (people from other companies whose names/titles fuzzy-matched). Driving
    the walk off `outbound_operators` (authoritative, 3 rows) instead of off the
    self-execs lead_sources avoids paying for those.
  - The company page also matters and is one fetch.

Usage:
    python3 engagers_research_own.py --client Acme [--max-pages 30]

Writes to: posts, post_engagements, leads, lead_sources, searches.
No disk artifacts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, load_client_env, normalize_linkedin_url, resolve_canonical_url  # noqa: E402

SALELEADS_HOST = "fresh-linkedin-scraper-api.p.rapidapi.com"
SKILL_NAME = "engagers-research-own"


def _load_key() -> str:
    for ln in open(os.path.expanduser("~/.env")).read().splitlines():
        if ln.startswith("FRESH_LINKEDIN_DATA_API_KEY"):
            return ln.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("FRESH_LINKEDIN_DATA_API_KEY missing")


def sl_get(path: str, params: dict, key: str) -> dict:
    url = f"https://{SALELEADS_HOST}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "x-rapidapi-host": SALELEADS_HOST, "x-rapidapi-key": key,
        "User-Agent": "curl/8.4.0", "Accept": "application/json",
    })
    for delay in (0, 10, 30):
        if delay: time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 403, 502, 503, 504): continue
            return {"_error": f"HTTP {e.code}"}
        except Exception as e:
            return {"_error": str(e)[:120]}
    return {"_error": "max retries"}


def paginate(path: str, base_params: dict, key: str, max_pages: int):
    """Yield items across pages. Uses pagination_token if returned by the
    endpoint, otherwise increments &page=<n>. Two pagination conventions in
    Saleleads coexist: /user/posts + /company/posts use pagination_token,
    /post/reactions + /post/comments use page=<n>."""
    cursor = None
    page = 1
    for _ in range(max_pages):
        params = dict(base_params)
        if cursor:
            params["pagination_token"] = cursor
        else:
            params["page"] = page
        r = sl_get(path, params, key)
        if r.get("_error") or not r.get("success"):
            return
        items = r.get("data") or []
        for it in items:
            yield it
        cursor = r.get("pagination_token")
        if cursor:
            time.sleep(0.3)
            continue
        # Page-based pagination — stop when items < 10 (small last page)
        if not items or len(items) < 10:
            return
        page += 1
        time.sleep(0.3)


def _numeric_post_id(urn_or_id: str) -> str:
    """Extract numeric activity id from urn:li:activity:<n>, urn:li:ugcPost:<n>
    or raw digits."""
    if not urn_or_id:
        return ""
    import re as _re
    m = _re.search(r"(\d{10,})", urn_or_id)
    return m.group(1) if m else urn_or_id


def _extract_post_urn(p: dict) -> str | None:
    """Saleleads /user/posts uses `id` (numeric) + `share_urn` (urn:li:ugcPost:...).
    Use share_urn primary, fall back to id-derived activity URN."""
    if p.get("urn"): return p["urn"]
    if p.get("post_urn"): return p["post_urn"]
    if p.get("share_urn"): return p["share_urn"]
    pid = p.get("id")
    return f"urn:li:activity:{pid}" if pid else None


def upsert_post(cur, p: dict) -> tuple[str, bool] | None:
    """Returns (post_uuid, was_inserted) or None if no URN extractable."""
    urn = _extract_post_urn(p)
    if not urn:
        return None
    cur.execute("""
        INSERT INTO posts (post_urn, post_url, poster_name, poster_linkedin_url,
                           posted_at, post_text, likes, comments_count, shares,
                           post_type, share_urn, raw_data, last_scraped_at)
        VALUES (%(urn)s, %(url)s, %(name)s, %(plu)s, %(at)s, %(txt)s,
                %(li)s, %(co)s, %(sh)s, %(typ)s, %(s_urn)s, %(raw)s::jsonb, now())
        ON CONFLICT (post_urn) DO UPDATE SET
            likes = COALESCE(EXCLUDED.likes, posts.likes),
            comments_count = COALESCE(EXCLUDED.comments_count, posts.comments_count),
            shares = COALESCE(EXCLUDED.shares, posts.shares),
            last_scraped_at = now()
        RETURNING id, (xmax = 0) AS inserted
    """, {
        "urn": urn,
        "url": p.get("post_url") or p.get("url") or "",
        "name": (p.get("author") or {}).get("name") or p.get("poster_name") or "",
        "plu": (p.get("author") or {}).get("url") or p.get("poster_linkedin_url") or "",
        "at": p.get("posted_at") or p.get("date") or None,
        "txt": (p.get("post_text") or p.get("text") or "")[:6000],
        "li": (p.get("reactions") or {}).get("total") or p.get("likes") or 0,
        "co": p.get("comments_count") or p.get("comments") or 0,
        "sh": p.get("shares") or 0,
        "typ": p.get("post_type") or "",
        "s_urn": p.get("share_urn"),
        "raw": json.dumps(p),
    })
    return cur.fetchone()


def upsert_lead_from_engager(cur, e: dict) -> str | None:
    profile = (e.get("profile") or e.get("actor") or {})
    li_url = profile.get("url") or profile.get("public_profile_url") or e.get("profile_url") or ""
    if not li_url: return None
    name = (f"{profile.get('first_name','')} {profile.get('last_name','')}".strip()
            or profile.get("name") or profile.get("full_name") or "")
    headline = profile.get("headline") or e.get("headline") or ""
    urn = profile.get("urn") or profile.get("member_urn")
    pubid = profile.get("public_identifier") or profile.get("username")
    li_url = resolve_canonical_url(cur, normalize_linkedin_url(li_url), urn_hint=urn)
    cur.execute("""
        INSERT INTO leads (linkedin_url, linkedin_urn, public_id, name, headline, last_enriched_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (linkedin_url) DO UPDATE SET
            linkedin_urn = COALESCE(EXCLUDED.linkedin_urn, leads.linkedin_urn),
            public_id    = COALESCE(EXCLUDED.public_id,    leads.public_id),
            name         = COALESCE(leads.name,            EXCLUDED.name),
            headline     = COALESCE(leads.headline,        EXCLUDED.headline),
            updated_at   = now()
        RETURNING id
    """, (li_url, urn, pubid, name or None, headline or None))
    return cur.fetchone()[0]


def write_engagement(cur, post_uuid: str, lead_uuid: str | None, kind: str, raw: dict):
    cur.execute("""
        INSERT INTO post_engagements (post_id, lead_id, engagement_type, raw_data, scraped_at)
        VALUES (%s, %s, %s, %s::jsonb, now())
        ON CONFLICT DO NOTHING
    """, (post_uuid, lead_uuid, kind, json.dumps(raw)))


def write_source(cur, lead_uuid: str, label: str, raw: dict):
    cur.execute("""
        INSERT INTO lead_sources (lead_id, source_type, source_label, source_date, raw_data)
        VALUES (%s, 'engagers_research', %s, %s, %s::jsonb)
    """, (lead_uuid, label, date.today(), json.dumps(raw)))


def write_search_record(cur, query: str, n_results: int):
    cur.execute("""
        INSERT INTO searches (skill, query, started_at, finished_at, n_results)
        VALUES (%s, %s, now(), now(), %s)
        RETURNING id
    """, (SKILL_NAME, query, n_results))
    return cur.fetchone()[0]


def walk_user(username: str, display_name: str, conn, key: str, max_pages: int):
    print(f"\n--- {display_name} ({username}) ---", flush=True)
    label = f"Acme (self) — engager on {display_name} post"
    posts_seen = posts_inserted = reactions = comments = skipped_no_urn = 0
    for post in paginate("/api/v1/user/posts", {"username": username}, key, max_pages):
        posts_seen += 1
        with conn.cursor() as cur:
            try:
                res = upsert_post(cur, post)
                if not res:
                    skipped_no_urn += 1
                    conn.rollback()
                    continue
                post_uuid, was_new = res
                if was_new: posts_inserted += 1
                urn = _extract_post_urn(post)
                for reactor in paginate("/api/v1/post/reactions", {"post_id": _numeric_post_id(urn)}, key, max_pages):
                    lid = upsert_lead_from_engager(cur, reactor)
                    if lid:
                        write_engagement(cur, post_uuid, lid, "reaction", reactor)
                        write_source(cur, lid, label, {"post_urn": urn, "engagement": "reaction"})
                        reactions += 1
                for commenter in paginate("/api/v1/post/comments", {"post_id": _numeric_post_id(urn)}, key, max_pages):
                    lid = upsert_lead_from_engager(cur, commenter)
                    if lid:
                        write_engagement(cur, post_uuid, lid, "comment", commenter)
                        write_source(cur, lid, label, {"post_urn": urn, "engagement": "comment"})
                        comments += 1
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"    ⚠ post {posts_seen}: {type(e).__name__}: {str(e)[:120]}", flush=True)
        if posts_seen % 10 == 0:
            print(f"    {posts_seen} posts processed, cumulative reactions={reactions} comments={comments}", flush=True)
    with conn.cursor() as cur:
        try:
            write_search_record(cur, f"user_posts_deep:{username}", posts_seen)
            conn.commit()
        except Exception:
            conn.rollback()
    print(f"  ✓ {display_name}: {posts_seen} posts ({posts_inserted} new, "
          f"{skipped_no_urn} skipped no-URN), {reactions} reactions, {comments} comments")


def walk_company(company_id: str, name: str, conn, key: str, max_pages: int):
    print(f"\n--- Company: {name} (id={company_id}) ---", flush=True)
    label = f"Acme (self) — engager on company page post"
    posts_seen = reactions = comments = skipped_no_urn = 0
    for post in paginate("/api/v1/company/posts", {"company_id": company_id}, key, max_pages):
        posts_seen += 1
        with conn.cursor() as cur:
            try:
                res = upsert_post(cur, post)
                if not res:
                    skipped_no_urn += 1
                    conn.rollback()
                    continue
                post_uuid, _ = res
                urn = _extract_post_urn(post)
                for reactor in paginate("/api/v1/post/reactions", {"post_id": _numeric_post_id(urn)}, key, max_pages):
                    lid = upsert_lead_from_engager(cur, reactor)
                    if lid:
                        write_engagement(cur, post_uuid, lid, "reaction", reactor)
                        write_source(cur, lid, label, {"post_urn": urn, "engagement": "reaction"})
                        reactions += 1
                for commenter in paginate("/api/v1/post/comments", {"post_id": _numeric_post_id(urn)}, key, max_pages):
                    lid = upsert_lead_from_engager(cur, commenter)
                    if lid:
                        write_engagement(cur, post_uuid, lid, "comment", commenter)
                        write_source(cur, lid, label, {"post_urn": urn, "engagement": "comment"})
                        comments += 1
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"    ⚠ post {posts_seen}: {type(e).__name__}: {str(e)[:120]}", flush=True)
    with conn.cursor() as cur:
        try:
            write_search_record(cur, f"company_posts_deep:{company_id}", posts_seen)
            conn.commit()
        except Exception:
            conn.rollback()
    print(f"  ✓ company {name}: {posts_seen} posts ({skipped_no_urn} skipped no-URN), "
          f"{reactions} reactions, {comments} comments")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default="Acme")
    ap.add_argument("--max-pages", type=int, default=30,
                    help="Max pagination pages per posts feed (20 items/page).")
    args = ap.parse_args()

    load_client_env(args.client)
    key = _load_key()

    # Founder usernames (deduced from Saleleads /user/posts probing) — hard-coded
    # to avoid driving off the noisy `Acme (self)` find_senior_execs rows.
    targets = [
        ("tolts",          "Dana Tolts"),
        ("eyar-zilberman", "Eyar Zilberman"),
        ("bob-labunsky", "Roman Labunsky"),
    ]

    with connect(args.client) as conn:
        for username, name in targets:
            try:
                walk_user(username, name, conn, key, args.max_pages)
            except Exception as e:
                print(f"  ✗ {name}: {type(e).__name__}: {e}", flush=True)

        # Company page — look up saleleads_id from companies table
        with conn.cursor() as cur:
            cur.execute("""
                SELECT saleleads_id, name FROM companies
                WHERE linkedin_slug='acme' OR name='Acme' LIMIT 1
            """)
            r = cur.fetchone()
        if r and r[0]:
            try:
                walk_company(r[0], r[1], conn, key, args.max_pages)
            except Exception as e:
                print(f"  ✗ company: {type(e).__name__}: {e}", flush=True)
        else:
            print("\n  ⚠ no Acme saleleads_id in companies table; skipping company-page walk")

    return 0


if __name__ == "__main__":
    sys.exit(main())
