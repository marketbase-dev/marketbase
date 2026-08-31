#!/usr/bin/env python3
"""Generic read-through / write-through cache for paid/external API calls,
backed by the existing `enrichment_calls` table. Implements the global
"never re-pay for scraping" rule (see ~/.claude/CLAUDE.md): every metered
fetch is keyed on (api, endpoint, normalized-params); a hit with a prior
success returns the cached raw response instead of calling again.

Usage:
    import api_cache
    data = api_cache.cached_call(conn, "saleleads", "/api/v1/post/reactions",
                                 {"post_id": pid}, lambda: fetch_reactions(pid))
"""
import json


def _canon(params: dict) -> str:
    return json.dumps({k: str(v) for k, v in sorted((params or {}).items())},
                      separators=(",", ":"))


def get(conn, api: str, endpoint: str, params: dict):
    """Return the cached response for a prior SUCCESSFUL call, else None.
    Note: a cached value of [] / {} is a real hit and is returned as-is, so
    callers must distinguish 'no cache row' (None) from 'cached empty'."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT response FROM enrichment_calls
               WHERE api=%s AND endpoint=%s AND params=%s::jsonb AND success
               ORDER BY fetched_at DESC LIMIT 1""",
            (api, endpoint, _canon(params)))
        row = cur.fetchone()
    return row[0] if row else None


def put(conn, api: str, endpoint: str, params: dict, success: bool,
        response, cost=None, error=None):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO enrichment_calls
                 (api, endpoint, params, success, response, cost, error_message, fetched_at)
               VALUES (%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s, now())""",
            (api, endpoint, _canon(params), success,
             json.dumps(response) if response is not None else None, cost, error))
    conn.commit()


def cached_call(conn, api: str, endpoint: str, params: dict, fetch_fn,
                *, use_cache: bool = True, cost=None):
    """Read-through/write-through. `fetch_fn` is a 0-arg callable returning the
    JSON-serializable response to cache and return. Only successful (non-None)
    responses are cached; a None from fetch_fn is treated as a miss (not cached
    as a hit), so transient failures retry next run."""
    if use_cache:
        hit = get(conn, api, endpoint, params)
        if hit is not None:
            return hit, True
    data = fetch_fn()
    if data is not None:
        put(conn, api, endpoint, params, True, data, cost=cost)
    return data, False
