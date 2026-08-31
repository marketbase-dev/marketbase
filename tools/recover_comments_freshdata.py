#!/usr/bin/env python3
"""Recover post commenters via the Fresh LinkedIn Profile Data API for posts
where Saleleads returned no/partial commenters.

Why this exists:
  - Saleleads `/post/comments` misses commenters (the std engagers pipeline
    captured ~0), and its parser reads the wrong person field. (memory:
    reference_saleleads_post_engagement_quirks — commenters need the URL-activity
    id + a `commenter` field.)
  - Fresh LinkedIn Profile Data `/get-post-comments?urn=<numeric activity id>`
    returns each comment with a `commenter` {name, linkedin_url, headline, urn},
    the comment `text`, and nested `replies` (each with its own commenter).

Captures BOTH top-level commenters and reply authors (full analysis). Every raw
page is write-through cached in enrichment_calls (never re-pay). Existing comment
engagements for a touched post are deleted and replaced with the fuller set.

Usage:
  python3 recover_comments_freshdata.py --client Acme --poster ben-seri \
      --source-label "Zafran Security (competitor)"
"""
from __future__ import annoacmens

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
from lib import load_client_env  # noqa: E402
import api_cache  # noqa: E402
from engagers_research import (  # noqa: E402
    upsert_lead_from_engagement,
    insert_engagement,
    insert_lead_source_engager,
)

HOST = "fresh-linkedin-profile-data.p.rapidapi.com"
API = "fresh-linkedin-profile-data"
ENDPOINT = "/get-post-comments"
PER_PAGE = 10


def _key() -> str:
    k = os.environ.get("FRESH_LINKEDIN_DATA_API_KEY", "")
    if not k:
        sys.exit("FRESH_LINKEDIN_DATA_API_KEY missing in env")
    return k


def _activity_ids(post_url: str | None, post_urn: str | None) -> list[str]:
    ids: list[str] = []
    for s in (post_url or "", post_urn or ""):
        m = re.search(r"(\d{15,25})", s)
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def _fetch_page(key: str, activity_id: str, page: int) -> dict | None:
    u = f"https://{HOST}{ENDPOINT}?" + urllib.parse.urlencode({"urn": activity_id, "page": page})
    req = urllib.request.Request(u, headers={"x-rapidapi-key": key, "x-rapidapi-host": HOST})
    for delay in (0, 10, 30):
        if delay:
            time.sleep(delay)
        try:
            return json.loads(urllib.request.urlopen(req, timeout=45).read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504):
                continue
            return {"_http": e.code}
        except Exception:
            continue
    return None


def fetch_all_comments(cache_conn, key: str, activity_id: str, ncomments: int,
                       use_cache: bool) -> list[dict]:
    out: list[dict] = []
    max_pages = min(60, (max(ncomments, 0) // PER_PAGE) + 2)
    page = 1
    while page <= max_pages:
        params = {"urn": activity_id, "page": page}
        data, _hit = api_cache.cached_call(
            cache_conn, API, ENDPOINT, params,
            lambda: _fetch_page(key, activity_id, page),
            use_cache=use_cache,
        )
        if not isinstance(data, dict) or data.get("_http"):
            break
        rows = data.get("data") or []
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        if len(rows) < PER_PAGE:
            break
        page += 1
    return out


def _iter_people(comments: list[dict]):
    """Yield (person, text, raw) for every top-level comment and nested reply."""
    for cm in comments:
        person = cm.get("commenter") or cm.get("user") or cm.get("actor")
        if person:
            yield person, (cm.get("text") or ""), cm
        for rep in (cm.get("replies") or []):
            rperson = rep.get("commenter") or rep.get("user") or rep.get("actor")
            if rperson:
                yield rperson, (rep.get("text") or ""), rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--poster", help="Restrict to one poster (linkedin url / vanity substring).")
    ap.add_argument("--source-label", help="lead_sources label for recovered commenters. "
                    "Default 'engager of <poster> posts (freshdata)'.")
    ap.add_argument("--min-missing", type=int, default=3,
                    help="Only touch posts missing at least this many comments (default 3).")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    cl = load_client_env(a.client)
    db = cl["GTM_DB_CONNSTRING"]
    key = _key()

    con = psycopg2.connect(db)
    cache = psycopg2.connect(db)
    con.autocommit = False

    where = "p.poster_linkedin_url ILIKE %(poster)s" if a.poster else "TRUE"
    poster_param = f"%{a.poster}%" if a.poster else None

    with con.cursor() as c:
        c.execute(f"""
            SELECT p.id, p.post_urn, p.post_url, p.poster_name, p.poster_linkedin_url,
                   COALESCE(p.comments_count,0) AS nc,
                   count(*) FILTER (WHERE pe.engagement_type='comment') AS cc
            FROM posts p
            LEFT JOIN post_engagements pe ON pe.post_id = p.id
            WHERE {where}
            GROUP BY p.id, p.post_urn, p.post_url, p.poster_name, p.poster_linkedin_url, p.comments_count
            HAVING COALESCE(p.comments_count,0) - count(*) FILTER (WHERE pe.engagement_type='comment') >= %(minmiss)s
            ORDER BY (COALESCE(p.comments_count,0) - count(*) FILTER (WHERE pe.engagement_type='comment')) DESC
        """, {"poster": poster_param, "minmiss": a.min_missing})
        targets = c.fetchall()

    print(f"[freshdata-comments] {len(targets)} incomplete posts to recover")
    totals = {"posts": 0, "comment_rows": 0}
    for i, (post_uuid, post_urn, post_url, pname, purl, nc, cc) in enumerate(targets, 1):
        cand_ids = _activity_ids(post_url, post_urn)
        if not cand_ids:
            print(f"  [{i}/{len(targets)}] no activity id, skip")
            continue
        comments, aid = [], cand_ids[0]
        for cid in cand_ids:
            comments = fetch_all_comments(cache, key, cid, nc, use_cache=not a.refresh)
            if comments:
                aid = cid
                break
        if not comments:
            print(f"  [{i}/{len(targets)}] comments={nc} had={cc} -> 0 commenters (ids={cand_ids})")
            continue
        label = a.source_label or f"engager of {pname or purl} posts (freshdata)"
        with con.cursor() as cur:
            cur.execute("DELETE FROM post_engagements WHERE post_id=%s AND engagement_type='comment'",
                        (post_uuid,))
            n = 0
            for person, text, raw in _iter_people(comments):
                lead_uuid = upsert_lead_from_engagement(cur, person)
                if lead_uuid:
                    insert_engagement(cur, post_uuid, lead_uuid, "comment", None, (text or "")[:4000], raw)
                    insert_lead_source_engager(cur, lead_uuid, label, raw)
                    n += 1
        con.commit()
        totals["posts"] += 1
        totals["comment_rows"] += n
        print(f"  [{i}/{len(targets)}] comments={nc} had={cc} -> {len(comments)} threads, {n} rows (urn={aid})")

    con.close(); cache.close()
    print(f"[freshdata-comments] DONE {totals}")


if __name__ == "__main__":
    main()
