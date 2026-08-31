#!/usr/bin/env python3
"""Cached LinkdAPI client for MarketBase tools.

LinkdAPI (https://linkdapi.com) is the reaction/likes backup source for posts
that Saleleads can't return reactors for (ugcPost / article-type posts) or
truncates. Its key value: GET /api/v1/posts/likes returns the actual list of
people who reacted, paginated by `start` offset (0,10,20,…), keyed on the
post's feed-activity numeric id.

Every page is cached read-through / write-through in the `enrichment_calls`
table keyed on (api, endpoint, params) — so a crash or re-run never re-pays for
a page already fetched (see ~/.claude/CLAUDE.md "Caching paid API responses").

Auth: header `X-linkdapi-apikey: <LINKDAPI_API_KEY>` (NOT x-api-key / Bearer —
those 401). Cloudflare blocks non-browser UAs, so a browser User-Agent is
required or every request 403s with "error code: 1010".
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error

API_NAME = "linkdapi"
BASE = "https://linkdapi.com"
_HEADERS_BASE = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/121.0 Safari/537.36"),
    "Accept": "application/json",
}


def _api_key() -> str:
    key = os.environ.get("LINKDAPI_API_KEY")
    if key:
        return key
    # Fall back to ~/.env so callers don't have to pre-load it.
    envp = os.path.expanduser("~/.env")
    if os.path.exists(envp):
        for line in open(envp):
            line = line.strip()
            if line.startswith("LINKDAPI_API_KEY=") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("LINKDAPI_API_KEY not set in env or ~/.env")


def _canon(params: dict) -> str:
    """Stable JSON for cache-key comparison (sorted keys, string values)."""
    return json.dumps({k: str(v) for k, v in sorted(params.items())}, separators=(",", ":"))


def _cache_get(conn, endpoint: str, params: dict):
    """Return the cached response dict for a prior SUCCESSFUL call, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT response FROM enrichment_calls
               WHERE api = %s AND endpoint = %s AND params = %s::jsonb AND success
               ORDER BY fetched_at DESC LIMIT 1""",
            (API_NAME, endpoint, _canon(params)),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _cache_put(conn, endpoint: str, params: dict, success: bool, response, cost, error=None):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO enrichment_calls
                 (api, endpoint, params, success, response, cost, error_message, fetched_at)
               VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, now())""",
            (API_NAME, endpoint, _canon(params), success,
             json.dumps(response) if response is not None else None,
             cost, error),
        )
    conn.commit()


def get(conn, endpoint: str, params: dict, *, retries: int = 5, use_cache: bool = True) -> dict | None:
    """Cached GET against LinkdAPI. `conn` is a live psycopg2 connection to the
    client MarketBase (where the cache lives). Returns the parsed JSON dict, or None
    on hard failure. Successful responses are cached; the next identical call is
    free."""
    if use_cache:
        cached = _cache_get(conn, endpoint, params)
        if cached is not None:
            return cached

    url = BASE + endpoint + "?" + urllib.parse.urlencode(params)
    headers = dict(_HEADERS_BASE, **{"X-linkdapi-apikey": _api_key()})
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            r = urllib.request.urlopen(req, timeout=60)
            data = json.loads(r.read())
            # LinkdAPI uses success:false in-band for "URN not found" etc. —
            # cache those too (a definitive negative we shouldn't re-pay for),
            # but NOT transient/HTTP errors.
            cost = (data.get("data") or {}).get("cost") if isinstance(data.get("data"), dict) else None
            _cache_put(conn, endpoint, params, bool(data.get("success")), data, cost)
            return data
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(8 * (attempt + 1))  # generous backoff so pagination isn't cut short
                continue
            # 4xx other than 429 → don't retry
            body = e.read()[:200]
            _cache_put(conn, endpoint, params, False, None, None, f"HTTP {e.code}: {body}")
            return None
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(5 * (attempt + 1))
    _cache_put(conn, endpoint, params, False, None, None, f"retries exhausted: {last_err}")
    return None


def post_likes(conn, activity_id: str, claimed_likes: int = 0, *, hard_cap_pages: int = 200) -> list[dict]:
    """Paginate ALL reactors on a post via /api/v1/posts/likes. Each page is
    cached, so a re-run is free for already-fetched pages. Returns a list of
    normalized person dicts: {url, name, headline, urn, reaction_type}."""
    seen, out = set(), []
    cap = (claimed_likes or 0) + 80  # over-fetch a little past the claimed count
    start, pages = 0, 0
    while start <= cap and pages < hard_cap_pages:
        d = get(conn, "/api/v1/posts/likes", {"urn": activity_id, "start": start})
        if not d or not d.get("success"):
            break
        likes = ((d.get("data") or {}).get("likes")) or []
        if not likes:
            break
        fresh = 0
        for l in likes:
            a = l.get("actor") or {}
            key = a.get("url") or a.get("urn")
            if not key or key in seen:
                continue
            seen.add(key)
            fresh += 1
            out.append({
                "url": a.get("url"),
                "name": a.get("name"),
                "headline": a.get("headline"),
                "urn": a.get("urn"),
                "reaction_type": l.get("reactionType") or l.get("reaction_type"),
            })
        if fresh == 0:
            break
        start += 10
        pages += 1
    return out
