#!/usr/bin/env python3
"""buyer-monitor engagers — run engager research against an explicit CSV of
target people (not the company->exec model of marketbase-engagers-research).

For each target in the CSV:
  1. UPSERT the target as a lead (source_type='buyer_monitor_target').
  2. Fetch their last N posts (default 20) via Saleleads /user/posts.
  3. For each post, pull every REACTOR (liker) via /post/reactions and persist
     to posts / post_engagements / leads / lead_sources.
  4. Record a `searches` row per target for idempotent resume.

Reactions only by default (the ask is "who liked"); pass --commenters to add
comments too. Run backfill_reactions_linkdapi.py afterwards to catch ugcPost /
article posts where Saleleads returns 0 reactors.

Usage:
  python3 buyer_monitor_engagers.py --client Acme --csv "/path/to.csv" \
      --source-label "Acme Buyer Monitor"
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engagers_research as er  # noqa: E402

SKILL = "acme-buyer-monitor"


def connect_with_retry(db_url: str, tries: int = 6, base_sleep: int = 10):
    """_fresh_conn, but ride out transient DNS / network blips instead of dying.
    A single failed Neon hostname lookup mid-sweep should never kill the run."""
    import time
    last = None
    for attempt in range(1, tries + 1):
        try:
            return er._fresh_conn(db_url)
        except Exception as e:
            last = e
            if attempt == tries:
                break
            sleep_s = base_sleep * attempt
            print(f"      ~ DB connect failed ({type(e).__name__}); retry {attempt}/{tries} in {sleep_s}s", flush=True)
            time.sleep(sleep_s)
    raise last


def load_env(path):
    p = os.path.expanduser(path); out = {}
    if not os.path.exists(p): return out
    for line in open(p):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def parse_csv(path: str) -> list[dict]:
    """The sheet has blank lead columns + a header row where col 8 == 'linkedin_url'.
    Layout: [_, first, last, job_title, company, country, state, city, linkedin_url]."""
    rows = list(csv.reader(open(path)))
    out, seen = [], set()
    for r in rows:
        if len(r) < 9:
            continue
        url = r[8].strip()
        if not url.lower().startswith("http"):
            continue  # skips header + blank rows
        canon = er.normalize_linkedin_url(url)
        if canon in seen:
            continue
        seen.add(canon)
        name = f"{r[1].strip()} {r[2].strip()}".strip()
        out.append({
            "url": canon, "name": name or None,
            "title": r[3].strip() or None, "company": r[4].strip() or None,
            "country": r[5].strip() or None, "city": (r[7].strip() or None),
        })
    return out


def upsert_target_lead(conn, t: dict, source_label: str):
    vanity = er._slug_from_url(t["url"])
    pubid = vanity if (vanity and not er.member_urn_token(vanity)) else None
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO leads (linkedin_url, public_id, name, current_title, current_company, city, country)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (linkedin_url) DO UPDATE SET
                public_id       = COALESCE(EXCLUDED.public_id,       leads.public_id),
                name            = COALESCE(leads.name,               EXCLUDED.name),
                current_title   = COALESCE(leads.current_title,      EXCLUDED.current_title),
                current_company = COALESCE(leads.current_company,    EXCLUDED.current_company),
                city            = COALESCE(leads.city,               EXCLUDED.city),
                country         = COALESCE(leads.country,            EXCLUDED.country),
                updated_at      = now()
            RETURNING id
        """, (t["url"], pubid, t["name"], t["title"], t["company"], t["city"], t["country"]))
        lead_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO lead_sources (lead_id, source_type, source_label, source_date, raw_data)
            VALUES (%s, 'buyer_monitor_target', %s, now()::date, %s::jsonb)
            ON CONFLICT DO NOTHING
        """, (lead_id, source_label, json.dumps(t)))
    conn.commit()
    return vanity


def recent_search_exists(conn, query: str, max_age_days: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM searches WHERE skill=%s AND query=%s
                       AND ran_at > now() - (%s || ' days')::interval LIMIT 1""",
                    (SKILL, query, str(max_age_days)))
        return cur.fetchone() is not None


def insert_search(conn, query: str, params: dict, total_posts: int):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO searches (skill, query, params, total_posts_returned)
                       VALUES (%s, %s, %s::jsonb, %s)""",
                    (SKILL, query, json.dumps(params), total_posts))
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--source-label", default="Buyer Monitor")
    ap.add_argument("--max-posts", type=int, default=20)
    ap.add_argument("--max-engagers-per-post", type=int, default=500)
    ap.add_argument("--commenters", action="store_true", help="Also pull commenters (default reactions only).")
    ap.add_argument("--max-age-days", type=int, default=14)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N targets (debug).")
    args = ap.parse_args()

    for k, v in load_env("~/.env").items():
        os.environ.setdefault(k, v)
    client_env = load_env(f"~/.env.{args.client}")
    db_url = client_env.get("GTM_DB_CONNSTRING") or client_env.get("DATABASE_URL")
    if not db_url:
        sys.exit(f"No GTM_DB_CONNSTRING in ~/.env.{args.client}")

    targets = parse_csv(args.csv)
    if args.limit:
        targets = targets[:args.limit]
    print(f"[buyer-monitor] {len(targets)} targets from CSV", flush=True)

    grand = {"targets": 0, "posts": 0, "reactions": 0, "comments": 0, "skipped": 0, "errors": 0}
    for i, t in enumerate(targets, 1):
        try:
            conn = connect_with_retry(db_url)
            try:
                vanity = upsert_target_lead(conn, t, args.source_label)
            finally:
                try: conn.close()
                except Exception: pass
            if not vanity:
                print(f"  [{i}/{len(targets)}] !! no vanity for {t['url']}", flush=True)
                continue

            query = f"user_posts:{vanity}"
            conn = connect_with_retry(db_url)
            try:
                skip = (not args.refresh) and recent_search_exists(conn, query, args.max_age_days)
            finally:
                conn.close()
            if skip:
                grand["skipped"] += 1
                print(f"  [{i}/{len(targets)}] {vanity}: recent pull <{args.max_age_days}d, skip", flush=True)
                continue

            # up to 3 pages (~10 posts/page on Saleleads), then cap at --max-posts.
            posts = er.fetch_user_posts(vanity, max_pages=3)[:args.max_posts]
            print(f"  [{i}/{len(targets)}] {vanity} ({t['name']}): {len(posts)} posts", flush=True)
            stats = er.harvest_posts_for(
                db_url, "user", posts, t["name"] or vanity, t["url"],
                source_label=f"{t['name'] or vanity} (buyer monitor)",
                max_engagers=args.max_engagers_per_post,
                include_reactors=True, include_commenters=args.commenters,
            )
            conn = connect_with_retry(db_url)
            try:
                insert_search(conn, query, {"vanity": vanity, "max_posts": args.max_posts, **stats}, len(posts))
            finally:
                conn.close()
            grand["targets"] += 1
            grand["posts"] += stats["posts"]
            grand["reactions"] += stats["reactions"]
            grand["comments"] += stats["comments"]
            print(f"        posts={stats['posts']} reactions={stats['reactions']} comments={stats['comments']}", flush=True)
        except Exception as e:
            # Never let one target kill the sweep — log and move on. No searches row
            # is written, so a later re-run retries this target cleanly.
            grand["errors"] += 1
            print(f"  [{i}/{len(targets)}] !! target failed, skipping: {type(e).__name__}: {str(e)[:160]}", flush=True)
            continue

    print(f"[buyer-monitor] DONE {grand}", flush=True)


if __name__ == "__main__":
    main()
