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


def _is_idle(conn) -> bool:
    """True when `conn` has no transaction open right now (psycopg 2 and 3
    both expose `info.transaction_status`, 0 = idle). False when unknown, so
    an odd connection object is simply left alone."""
    try:
        return int(conn.info.transaction_status) == 0
    except Exception:
        return False


def _release(conn, was_idle: bool) -> None:
    """End the transaction a cache READ opened, so the wait that follows it
    (the vendor call, or whatever the caller does next) is not spent "idle in
    transaction".

    Why it matters: a serverless Postgres that suspends when idle (Neon, for
    one) treats a connection sitting inside an open transaction as active, and
    keeps the database awake and billed for as long as it sits there. A plainly
    idle connection lets it suspend. On any Postgres, an open transaction also
    holds back vacuum.

    Only when the connection was idle BEFORE the read: then the transaction
    holds nothing but our own SELECT, and ending it cannot commit or discard
    anything of the caller's. A caller with work pending keeps its transaction
    exactly as it was."""
    if not was_idle:
        return
    try:
        conn.commit()
    except Exception:
        pass          # releasing is a saving, never worth failing a read over


def _canon(params: dict) -> str:
    return json.dumps({k: str(v) for k, v in sorted((params or {}).items())},
                      separators=(",", ":"))


def get(conn, api: str, endpoint: str, params: dict):
    """Return the cached response for a prior SUCCESSFUL call, else None.
    Note: a cached value of [] / {} is a real hit and is returned as-is, so
    callers must distinguish 'no cache row' (None) from 'cached empty'."""
    was_idle = _is_idle(conn)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT response FROM enrichment_calls
               WHERE api=%s AND endpoint=%s AND params=%s::jsonb AND success
               ORDER BY fetched_at DESC LIMIT 1""",
            (api, endpoint, _canon(params)))
        row = cur.fetchone()
    _release(conn, was_idle)
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
