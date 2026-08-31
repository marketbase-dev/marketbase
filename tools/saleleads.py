#!/usr/bin/env python3
"""Unified Saleleads client — `fresh-linkedin-scraper-api` on RapidAPI.

One place that bakes in every gotcha we kept re-hitting:

  • **Cloudflare 1010 bot-block** (HTTP 403, body `error code: 1010`) → the API
    sits behind Cloudflare which bans the default `Python-urllib` User-Agent.
    We always send a real browser UA. This was the cause of the "403 Forbidden"
    that looked like a plan/quota problem but wasn't.
  • **20 req/min plan rate limit** → built-in process-wide token-bucket throttle
    (`rpm`, default 20). Shared across threads.
  • **429 / 5xx / transient Cloudflare** → retry with capped backoff.
  • **Never re-pay** (CLAUDE.md rule) → optional read-through/write-through cache
    via `api_cache` when you pass `cache_conn`.

Endpoints wrapped (GET unless noted):
    search_posts(keyword, page)             /api/v1/search/posts
    post_reactions(post_id, page)           /api/v1/post/reactions
    post_comments(post_id, page)            /api/v1/post/comments
    user_profile(username)                  /api/v1/user/profile
    user_posts(username, page)              /api/v1/user/posts
    company_posts(company_id, page)         /api/v1/company/posts
    company_profile(name)                   /api/v1/company-profile

Plus `*_all()` paginators that walk pages until empty / capped.

Usage:
    import saleleads
    d = saleleads.search_posts("cloud security", page=1)          # one page
    posts = saleleads.search_posts_all("cloud security", max_pages=40)
    # cached (never re-pay), pass a psycopg conn:
    d = saleleads.post_reactions(pid, cache_conn=conn)
"""
from __future__ import annoacmens
import json, os, threading, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HOST = "fresh-linkedin-scraper-api.p.rapidapi.com"
BASE = f"https://{HOST}"
API = "saleleads"  # api_cache namespace
# A real browser UA — REQUIRED, else Cloudflare returns 403 "error code: 1010".
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DEFAULT_RPM = 20  # current plan: 20 requests / minute

# ── process-wide rate limiter (token bucket, thread-safe) ────────────────────
_rate_lock = threading.Lock()
_next_ok = [0.0]


def _throttle(rpm: int) -> None:
    interval = 60.0 / max(1, rpm)
    with _rate_lock:
        now = time.monotonic()
        wait = _next_ok[0] - now
        if wait > 0:
            time.sleep(wait)
        _next_ok[0] = max(now, _next_ok[0]) + interval


def _api_key() -> str:
    k = os.environ.get("FRESH_LINKEDIN_DATA_API_KEY")
    if k:
        return k
    p = Path.home() / ".env"
    if p.exists():
        for ln in p.read_text().splitlines():
            if ln.startswith("FRESH_LINKEDIN_DATA_API_KEY"):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _request(method: str, path: str, params: dict | None = None,
             body: dict | None = None, *, rpm: int = DEFAULT_RPM,
             retries: int = 6) -> dict:
    """Raw request with UA, throttle, and retry. Returns parsed JSON, or
    {'success': False, '_error': ...} on hard failure."""
    key = _api_key()
    if not key:
        return {"success": False, "_error": "no FRESH_LINKEDIN_DATA_API_KEY"}
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"x-rapidapi-host": HOST, "x-rapidapi-key": key,
               "User-Agent": USER_AGENT, "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    last = ""
    for attempt in range(retries):
        _throttle(rpm)
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            btxt = ""
            try: btxt = e.read()[:200].decode(errors="ignore")
            except Exception: pass
            last = f"HTTP {e.code} {btxt}"
            # 429 rate-limit, 5xx, or a Cloudflare bounce (1010/1015) → back off + retry
            if e.code in (429, 500, 502, 503, 504) or "1010" in btxt or "1015" in btxt:
                time.sleep(min(6 * (attempt + 1), 45)); continue
            return {"success": False, "_error": last}
        except Exception as ex:
            last = str(ex)[:120]
            time.sleep(3 * (attempt + 1))
    return {"success": False, "_error": f"exhausted retries: {last}"}


def _cached(cache_conn, endpoint: str, params: dict, fetch, use_cache: bool):
    if cache_conn is not None and use_cache:
        import api_cache
        resp, _hit = api_cache.cached_call(cache_conn, API, endpoint, params, fetch)
        return resp if resp is not None else {"success": False, "_error": "null"}
    return fetch()


# ── single-page endpoint methods ─────────────────────────────────────────────
def search_posts(keyword: str, page: int = 1, *, cache_conn=None,
                 rpm: int = DEFAULT_RPM, use_cache: bool = True) -> dict:
    p = {"keyword": keyword, "page": page}
    return _cached(cache_conn, "/api/v1/search/posts", p,
                   lambda: _request("GET", "/api/v1/search/posts", p, rpm=rpm), use_cache)


def post_reactions(post_id: str, page: int = 1, *, cache_conn=None,
                   rpm: int = DEFAULT_RPM, use_cache: bool = True) -> dict:
    p = {"post_id": post_id, "page": page}
    return _cached(cache_conn, "/api/v1/post/reactions", p,
                   lambda: _request("GET", "/api/v1/post/reactions", p, rpm=rpm), use_cache)


def post_comments(post_id: str, page: int = 1, *, cache_conn=None,
                  rpm: int = DEFAULT_RPM, use_cache: bool = True) -> dict:
    p = {"post_id": post_id, "page": page}
    return _cached(cache_conn, "/api/v1/post/comments", p,
                   lambda: _request("GET", "/api/v1/post/comments", p, rpm=rpm), use_cache)


def user_profile(username: str, *, cache_conn=None, rpm: int = DEFAULT_RPM,
                 use_cache: bool = True) -> dict:
    p = {"username": username}
    return _cached(cache_conn, "/api/v1/user/profile", p,
                   lambda: _request("GET", "/api/v1/user/profile", p, rpm=rpm), use_cache)


def user_posts(username: str, page: int = 1, *, cache_conn=None,
               rpm: int = DEFAULT_RPM, use_cache: bool = True) -> dict:
    p = {"username": username, "page": page}
    return _cached(cache_conn, "/api/v1/user/posts", p,
                   lambda: _request("GET", "/api/v1/user/posts", p, rpm=rpm), use_cache)


def company_posts(company_id: str, page: int = 1, *, cache_conn=None,
                  rpm: int = DEFAULT_RPM, use_cache: bool = True) -> dict:
    p = {"company_id": company_id, "page": page}
    return _cached(cache_conn, "/api/v1/company/posts", p,
                   lambda: _request("GET", "/api/v1/company/posts", p, rpm=rpm), use_cache)


def company_profile(name: str, *, cache_conn=None, rpm: int = DEFAULT_RPM,
                    use_cache: bool = True) -> dict:
    p = {"name": name}
    return _cached(cache_conn, "/api/v1/company-profile", p,
                   lambda: _request("GET", "/api/v1/company-profile", p, rpm=rpm), use_cache)


# ── paginators (walk until empty / <10 / capped) ─────────────────────────────
def _paginate(fetch_page, max_pages: int) -> list:
    out, page = [], 1
    while page <= max_pages:
        d = fetch_page(page)
        if not d or not d.get("success", True):
            break
        data = d.get("data") or d.get("posts") or d.get("reactions") or d.get("comments") or []
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < 10:
            break
        page += 1
    return out


def search_posts_all(keyword: str, max_pages: int = 50, **kw) -> list:
    return _paginate(lambda pg: search_posts(keyword, pg, **kw), max_pages)


def post_reactions_all(post_id: str, max_pages: int = 50, **kw) -> list:
    return _paginate(lambda pg: post_reactions(post_id, pg, **kw), max_pages)


def post_comments_all(post_id: str, max_pages: int = 50, **kw) -> list:
    return _paginate(lambda pg: post_comments(post_id, pg, **kw), max_pages)


def user_posts_all(username: str, max_pages: int = 10, **kw) -> list:
    return _paginate(lambda pg: user_posts(username, pg, **kw), max_pages)


if __name__ == "__main__":
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "cloud security"
    d = search_posts(kw, 1)
    print(f"search_posts({kw!r}): success={d.get('success')} "
          f"posts={len(d.get('data') or [])} total={d.get('total')} err={d.get('_error')}")
