#!/usr/bin/env python3
"""Stage 2 of the post-engagement pipeline: harvest the reactor + commenter
LISTS for a poster's posts from Saleleads, into a client's MarketBase.

Primary engager-list source (LinkdAPI backfills the ugc/article reactions
Saleleads can't return — run backfill_reactions_linkdapi.py after this).

Correctness notes baked in (see reference_saleleads_post_engagement_quirks):
  - Two candidate ids per post (post_urn numeric vs the post_url feed-activity
    numeric); ugc vs activity posts key engagement on different ids, so we try
    both per endpoint.
  - The comment payload nests the person under `commenter` (url/name/
    description) NOT user/actor, and text under `comment`; nested `replies`
    each carry their own commenter. engagers_research.py misses all of these.

Idempotent: per post, existing reaction+comment engagements are deleted then
re-inserted in one pass (re-runs converge, no duplicates).

Usage:
  python3 harvest_post_engagers.py --client Acme-AI --profile alonrosenberg [--only-new]
"""
from __future__ import annoacmens
import argparse, os, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engagers_research as er
import api_cache


def load_env(p):
    p = os.path.expanduser(p); out = {}
    if not os.path.exists(p): return out
    for line in open(p):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def url_activity_num(u):
    m = re.search(r"urn:li:activity:(\d+)", u or ""); return m.group(1) if m else None

def norm_commenter(c):
    if not isinstance(c, dict): return None
    return {"url": c.get("url") or c.get("profile_url"), "name": c.get("name"),
            "headline": c.get("description") or c.get("headline")}

def iter_commenters(cm):
    yield norm_commenter(cm.get("commenter")), (cm.get("comment") or cm.get("text") or "")
    for rep in cm.get("replies", []) or []:
        yield norm_commenter(rep.get("commenter")), (rep.get("comment") or rep.get("text") or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--profile", required=True, help="poster linkedin /in/ slug")
    ap.add_argument("--only-new", action="store_true",
                    help="Ingest latest posts and harvest engagers ONLY for posts not already in the DB.")
    ap.add_argument("--refresh", action="store_true",
                    help="Bypass the raw-response cache and re-fetch from Saleleads.")
    args = ap.parse_args()

    for k, v in load_env("~/.env").items(): os.environ.setdefault(k, v)
    db_url = load_env(f"~/.env.{args.client}").get("GTM_DB_CONNSTRING")
    label = f"engager of {args.profile} posts"

    import psycopg2
    con = psycopg2.connect(db_url)
    cache = psycopg2.connect(db_url)   # dedicated conn for the raw-response cache
    use_cache = not args.refresh

    # Snapshot existing post_urns BEFORE ingest so we can tell which are new.
    with con.cursor() as cur:
        cur.execute("SELECT post_urn FROM posts WHERE poster_linkedin_url ILIKE %s",
                    (f"%{args.profile}%",))
        before = {r[0] for r in cur.fetchall()}

    # Ingest latest posts (idempotent upsert) so new posts are captured.
    poster_url = f"https://www.linkedin.com/in/{args.profile}"
    user_posts, _ = api_cache.cached_call(
        cache, "saleleads", "/api/v1/user/posts", {"username": args.profile},
        lambda: er.fetch_user_posts(args.profile), use_cache=use_cache)
    for p in (user_posts or [])[:40]:
        conn = er._fresh_conn(db_url)
        try:
            with conn.cursor() as cur:
                er.upsert_post(cur, p, args.profile, poster_url)
            conn.commit()
        except Exception:
            try: conn.rollback()
            except Exception: pass
        finally:
            try: conn.close()
            except Exception: pass

    with con.cursor() as cur:
        cur.execute("""SELECT id, post_urn, post_url FROM posts
                       WHERE poster_linkedin_url ILIKE %s ORDER BY posted_at DESC NULLS LAST""",
                    (f"%{args.profile}%",))
        allposts = cur.fetchall()
    con.close()

    targets = [(pid, pu, purl) for (pid, pu, purl) in allposts
               if (not args.only_new) or (pu not in before)]
    print(f"[harvest] {len(targets)} posts to harvest "
          f"({'new only' if args.only_new else 'all'})", flush=True)

    totals = {"reactions": 0, "comments": 0}
    for i, (post_uuid, post_urn, post_url) in enumerate(targets, 1):
        id_a = er._numeric_post_id(post_urn or "")
        id_b = url_activity_num(post_url)
        # idempotent: clear this post's reactions+comments first
        conn = er._fresh_conn(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM post_engagements WHERE post_id=%s AND engagement_type IN ('reaction','comment')",
                            (post_uuid,))
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass

        # reactions (dual-id), cached on the post's primary id
        def _rx():
            out = er.fetch_post_reactions(id_a) if id_a else []
            if not out and id_b and id_b != id_a: out = er.fetch_post_reactions(id_b)
            return out
        def _cm():
            out = er.fetch_post_comments(id_a) if id_a else []
            if not out and id_b and id_b != id_a: out = er.fetch_post_comments(id_b)
            return out
        rx, _ = api_cache.cached_call(cache, "saleleads", "/api/v1/post/reactions",
                                      {"post_id": id_a or id_b}, _rx, use_cache=use_cache)
        cm, _ = api_cache.cached_call(cache, "saleleads", "/api/v1/post/comments",
                                      {"post_id": id_a or id_b}, _cm, use_cache=use_cache)
        rx = rx or []; cm = cm or []

        conn = er._fresh_conn(db_url)
        try:
            for r in rx:
                person = r.get("user") or r.get("actor") or r
                for attempt in (1, 2):
                    try:
                        with conn.cursor() as cur:
                            lid = er.upsert_lead_from_engagement(cur, person)
                            if lid:
                                er.insert_engagement(cur, post_uuid, lid, "reaction",
                                                     r.get("reaction_type") or r.get("type"), None, r)
                                er.insert_lead_source_engager(cur, lid, label, r)
                                totals["reactions"] += 1
                        conn.commit(); break
                    except Exception:
                        try: conn.rollback()
                        except Exception: pass
                        if attempt == 1: conn = er._ensure_conn(conn, db_url)
            for c in cm:
                for person, text in iter_commenters(c):
                    if not person or not person.get("url"): continue
                    for attempt in (1, 2):
                        try:
                            with conn.cursor() as cur:
                                lid = er.upsert_lead_from_engagement(cur, person)
                                if lid:
                                    er.insert_engagement(cur, post_uuid, lid, "comment", None,
                                                         (text[:4000] if text else None), c)
                                    er.insert_lead_source_engager(cur, lid, label, c)
                                    totals["comments"] += 1
                            conn.commit(); break
                        except Exception:
                            try: conn.rollback()
                            except Exception: pass
                            if attempt == 1: conn = er._ensure_conn(conn, db_url)
        finally:
            try: conn.close()
            except Exception: pass
        if i % 5 == 0 or i == len(targets):
            print(f"  [{i}/{len(targets)}] reactions={totals['reactions']} comments={totals['comments']}", flush=True)

    print(f"[harvest] DONE {totals}", flush=True)


if __name__ == "__main__":
    main()
