#!/usr/bin/env python3
"""Backfill post reactors from LinkdAPI for posts where Saleleads couldn't
return them (ugcPost / article-type posts) or truncated the list.

This is the reaction BACKUP for building engager reports. Saleleads stays the
primary source (cheaper, no per-page Cloudflare dance); this tool fills the
gap afterward. Run it after `marketbase-engagers-research` whenever a poster has
article-type posts.

Idempotent + cached:
  - Every LinkdAPI page is cached in `enrichment_calls` (linkdapi.py), so
    re-runs never re-pay for fetched pages.
  - Per post: reactor rows are rewritten in one transaction (delete existing
    'reaction' engagements for that post, re-insert the full LinkdAPI set), so
    re-running converges instead of duplicating.
  - A `searches` row records each completed post pull.

Targets: posts whose captured reactions < claimed likes (the incomplete ones).
Scope with --poster (a linkedin url/vanity) or default to ALL incomplete posts.

Usage:
  python3 backfill_reactions_linkdapi.py --client Acme-AI
  python3 backfill_reactions_linkdapi.py --client Acme-AI --poster alonrosenberg
  python3 backfill_reactions_linkdapi.py --client Acme-AI --min-missing 5
"""
from __future__ import annoacmens

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import linkdapi  # noqa: E402
import engagers_research as er  # reuse upsert_lead_from_engagement / insert_engagement / conn helpers  # noqa: E402


def load_env(path):
    p = os.path.expanduser(path); out = {}
    if not os.path.exists(p): return out
    for line in open(p):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def url_activity_num(u: str) -> str | None:
    m = re.search(r"urn:li:activity:(\d+)", u or "")
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--poster", help="Restrict to one poster (linkedin url or vanity substring).")
    ap.add_argument("--min-missing", type=int, default=1,
                    help="Only backfill posts missing at least this many reactors (default 1).")
    ap.add_argument("--source-label", default=None,
                    help="lead_sources label. Default: 'engager of <poster> posts (linkdapi)'.")
    args = ap.parse_args()

    # Load global env (LINKDAPI key) then client DB.
    for k, v in load_env("~/.env").items():
        os.environ.setdefault(k, v)
    client_env = load_env(f"~/.env.{args.client}")
    db_url = client_env.get("GTM_DB_CONNSTRING") or client_env.get("DATABASE_URL")
    if not db_url:
        sys.exit(f"No GTM_DB_CONNSTRING in ~/.env.{args.client}")

    import psycopg2
    where = "p.poster_linkedin_url ILIKE %(poster)s" if args.poster else "TRUE"
    poster_param = f"%{args.poster}%" if args.poster else None

    con = psycopg2.connect(db_url)
    with con.cursor() as cur:
        cur.execute(f"""
            SELECT p.id, p.post_url, p.poster_name, p.poster_linkedin_url, p.likes,
                   count(*) FILTER (WHERE pe.engagement_type='reaction') AS rx
            FROM posts p
            LEFT JOIN post_engagements pe ON pe.post_id = p.id
            WHERE {where}
            GROUP BY p.id, p.post_url, p.poster_name, p.poster_linkedin_url, p.likes
            HAVING (p.likes > 0)
               AND (p.likes - count(*) FILTER (WHERE pe.engagement_type='reaction')) >= %(minmiss)s
            ORDER BY (p.likes - count(*) FILTER (WHERE pe.engagement_type='reaction')) DESC
        """, {"poster": poster_param, "minmiss": args.min_missing})
        targets = cur.fetchall()
    con.close()

    print(f"[backfill] {len(targets)} incomplete posts to backfill via LinkdAPI", flush=True)
    totals = {"posts": 0, "reactions": 0, "from_cache_pages": 0}

    # One connection for cache reads/writes + DB writes.
    cache_conn = er._fresh_conn(db_url)
    try:
        for i, (post_uuid, post_url, poster_name, poster_url, likes, rx_have) in enumerate(targets, 1):
            activity_id = url_activity_num(post_url)
            if not activity_id:
                print(f"  [{i}/{len(targets)}] !! no activity id in {post_url}", flush=True)
                continue
            label = args.source_label or f"engager of {poster_name or poster_url} posts (linkdapi)"
            reactors = linkdapi.post_likes(cache_conn, activity_id, likes)
            print(f"  [{i}/{len(targets)}] likes={likes} had={rx_have} -> LinkdAPI {len(reactors)} reactors "
                  f"(urn={activity_id})", flush=True)
            if not reactors:
                continue
            # Rewrite this post's reaction rows in one transaction (idempotent).
            wconn = er._fresh_conn(db_url)
            try:
                with wconn.cursor() as cur:
                    cur.execute("DELETE FROM post_engagements WHERE post_id=%s AND engagement_type='reaction'",
                                (post_uuid,))
                wconn.commit()
                n = 0
                for person in reactors:
                    for attempt in (1, 2):
                        try:
                            with wconn.cursor() as cur:
                                lead_uuid = er.upsert_lead_from_engagement(cur, person)
                                if lead_uuid:
                                    er.insert_engagement(cur, post_uuid, lead_uuid, "reaction",
                                                         person.get("reaction_type"), None, person)
                                    er.insert_lead_source_engager(cur, lead_uuid, label, person)
                                    n += 1
                            wconn.commit()
                            break
                        except Exception as e:
                            try: wconn.rollback()
                            except Exception: pass
                            if attempt == 1:
                                wconn = er._ensure_conn(wconn, db_url)
                            else:
                                print(f"      ! insert failed: {type(e).__name__}: {str(e)[:100]}", flush=True)
                # Record a searches row for idempotent audit.
                with wconn.cursor() as cur:
                    cur.execute("""INSERT INTO searches (skill, query, params, total_posts_returned)
                                   VALUES ('backfill-reactions-linkdapi', %s, %s::jsonb, 1)""",
                                (f"post_likes:{activity_id}",
                                 '{"activity_id":"%s","reactors":%d}' % (activity_id, n)))
                wconn.commit()
                totals["reactions"] += n
                totals["posts"] += 1
                print(f"      wrote {n} reactors", flush=True)
            finally:
                try: wconn.close()
                except Exception: pass
    finally:
        try: cache_conn.close()
        except Exception: pass

    print(f"[backfill] DONE {totals}", flush=True)


if __name__ == "__main__":
    main()
