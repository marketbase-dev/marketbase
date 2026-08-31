#!/usr/bin/env python3
"""Recover post reactors via the Fresh LinkedIn Profile Data API for posts where
Saleleads returned a partial/empty reactor set (the ugcPost / personal-post gap).

Why this exists:
  - Saleleads `/post/reactions` returns 0 (or partial) reactors on personal /
    ugcPost posts even when the post has many likes.
  - LinkdAPI (`backfill_reactions_linkdapi.py`) is the documented backup but is
    credit-blocked in practice → returns 0.
  - Fresh LinkedIn Profile Data `/get-post-reactions?urn=<numeric activity id>`
    returns the real reactors (name + linkedin_url + headline + reaction type),
    is not credit-blocked, and paginates 50/page.  (memory:
    reference_fresh_data_post_reactions — feed the numeric activity id from the
    post URL, NOT the share_urn.)

Every raw page is write-through cached in enrichment_calls
(api='fresh-linkedin-profile-data', endpoint='/get-post-reactions') so a crash /
re-run never re-pays (CLAUDE.md "never re-pay for scraping").

Scope selection mirrors backfill_reactions_linkdapi.py: --poster (linkedin url /
vanity substring) or ALL posts; only posts whose (likes - captured_reactions) >=
--min-missing are touched.  Existing reaction engagements for a touched post are
deleted and replaced with the fuller Fresh-data set (avoids double counting).

Usage:
  python3 recover_reactions_freshdata.py --client Acme --poster ben-seri
  python3 recover_reactions_freshdata.py --client Acme \
      --poster zafran-security --source-label "Zafran Security (competitor)"
"""
from __future__ import annotations

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
# Reuse the exact same lead canonicalization / engagement writers as the main
# engagers pipeline so recovered reactors dedupe against everything else.
from engagers_research import (  # noqa: E402
    upsert_lead_from_engagement,
    insert_engagement,
    insert_lead_source_engager,
)

HOST = "fresh-linkedin-profile-data.p.rapidapi.com"
API = "fresh-linkedin-profile-data"
ENDPOINT = "/get-post-reactions"
PER_PAGE = 50


def _key() -> str:
    k = os.environ.get("FRESH_LINKEDIN_DATA_API_KEY", "")
    if not k:
        sys.exit("FRESH_LINKEDIN_DATA_API_KEY missing in env")
    return k


def _activity_ids(post_url: str | None, post_urn: str | None) -> list[str]:
    """Candidate numeric activity ids, in priority order. LinkedIn sometimes
    stores a post's reactions under the URL-embedded activity id and sometimes
    under the (different) share/activity id in post_urn, so we try both."""
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
            # 400 = invalid urn / no reactions for this id → not retryable
            return {"_http": e.code}
        except Exception:
            continue
    return None


def fetch_all_reactors(cache_conn, key: str, activity_id: str, likes: int,
                       use_cache: bool) -> list[dict]:
    """Paginate until a short page, or we've collected >= likes, or a page cap."""
    reactors: list[dict] = []
    max_pages = min(40, (max(likes, 0) // PER_PAGE) + 2)
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
        reactors.extend(rows)
        if len(rows) < PER_PAGE:
            break
        if likes and len(reactors) >= likes:
            break
        page += 1
    return reactors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--poster", help="Restrict to one poster (linkedin url / vanity substring).")
    ap.add_argument("--source-label", help="lead_sources label for recovered reactors. "
                    "Default 'engager of <poster> posts (freshdata)'.")
    ap.add_argument("--min-missing", type=int, default=5,
                    help="Only touch posts missing at least this many reactors (default 5).")
    ap.add_argument("--refresh", action="store_true",
                    help="Bypass the enrichment_calls cache and re-fetch pages.")
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
            SELECT p.id, p.post_urn, p.post_url, p.poster_name, p.poster_linkedin_url, p.likes,
                   count(*) FILTER (WHERE pe.engagement_type='reaction') AS rx
            FROM posts p
            LEFT JOIN post_engagements pe ON pe.post_id = p.id
            WHERE {where}
            GROUP BY p.id, p.post_urn, p.post_url, p.poster_name, p.poster_linkedin_url, p.likes
            HAVING COALESCE(p.likes,0) - count(*) FILTER (WHERE pe.engagement_type='reaction') >= %(minmiss)s
            ORDER BY (COALESCE(p.likes,0) - count(*) FILTER (WHERE pe.engagement_type='reaction')) DESC
        """, {"poster": poster_param, "minmiss": a.min_missing})
        targets = c.fetchall()

    print(f"[freshdata] {len(targets)} incomplete posts to recover")
    totals = {"posts": 0, "reactors": 0, "new_rows": 0}
    for i, (post_uuid, post_urn, post_url, pname, purl, likes, rx_have) in enumerate(targets, 1):
        cand_ids = _activity_ids(post_url, post_urn)
        if not cand_ids:
            print(f"  [{i}/{len(targets)}] no activity id, skip")
            continue
        reactors, aid = [], cand_ids[0]
        for cid in cand_ids:
            reactors = fetch_all_reactors(cache, key, cid, likes or 0, use_cache=not a.refresh)
            if reactors:
                aid = cid
                break
        if not reactors:
            print(f"  [{i}/{len(targets)}] likes={likes} had={rx_have} -> 0 reactors (ids={cand_ids})")
            continue
        label = a.source_label or f"engager of {pname or purl} posts (freshdata)"
        with con.cursor() as cur:
            # Replace the partial set so counts don't double.
            cur.execute("DELETE FROM post_engagements WHERE post_id=%s AND engagement_type='reaction'",
                        (post_uuid,))
            n = 0
            for r in reactors:
                person = r.get("reactor") or r.get("user") or r
                rtype = r.get("type") or r.get("reaction_type")
                lead_uuid = upsert_lead_from_engagement(cur, person)
                if lead_uuid:
                    insert_engagement(cur, post_uuid, lead_uuid, "reaction", rtype, None, r)
                    insert_lead_source_engager(cur, lead_uuid, label, r)
                    n += 1
        con.commit()
        totals["posts"] += 1
        totals["reactors"] += len(reactors)
        totals["new_rows"] += n
        print(f"  [{i}/{len(targets)}] likes={likes} had={rx_have} -> {len(reactors)} reactors "
              f"({n} rows) (urn={aid})")

    con.close(); cache.close()
    print(f"[freshdata] DONE {totals}")


if __name__ == "__main__":
    main()
