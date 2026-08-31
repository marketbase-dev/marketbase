#!/usr/bin/env python3
"""For every company in a client's MarketBase (competitors + self by default), fetch
each ingested senior exec's recent LinkedIn posts + the engagers on each post
(reactors + commenters) and write everything to Postgres.

Source of execs: `lead_sources WHERE source_type='find_senior_execs'`.
Source of company posts: `v_competitor_companies` + (optionally) self-tagged
companies via `company_relationships(relationship='self')`.

API: Saleleads (`fresh-linkedin-scraper-api.p.rapidapi.com`) endpoints
  /api/v1/user/posts?username=<vanity>&page=<n>
  /api/v1/company/posts?company_id=<id>&page=<n>
  /api/v1/post/reactions?post_id=<numeric>&page=<n>
  /api/v1/post/comments?post_id=<numeric>&page=<n>

Idempotency: a `searches` row is written for every per-exec / per-company /
per-post call we make; re-runs skip queries we've already executed within
`--max-age-days` unless `--refresh` is set.

No files written — everything persists in Postgres.
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
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
from lib import (normalize_linkedin_url, load_client_env, database_url,  # noqa: E402
                 resolve_canonical_url, member_urn_token)
import api_cache  # noqa: E402


SL_HOST = "fresh-linkedin-scraper-api.p.rapidapi.com"
SL_BASE = f"https://{SL_HOST}"

SKILL_NAME = "engagers-research"


def _api_key() -> str:
    return os.environ.get("FRESH_LINKEDIN_DATA_API_KEY", "")


# ── Raw-response archive (CLAUDE.md "never re-pay for scraping" rule) ──────────
# Write-through ONLY: every raw Saleleads page is stashed in enrichment_calls so
# a later stage / re-run after a parsing bug can re-derive without re-fetching
# (e.g. recover commenters this tool's parser drops). It does NOT read-through to
# gate fetches — that stays governed by the `searches` 14-day window, so periodic
# refreshes still re-pull and capture NEW engagers. Best-effort: a cache-write
# failure (e.g. a dropped DB conn) never breaks the harvest.
_CACHE_CONN = None
_CACHE_DB_URL = None


def _init_archive(db_url: str):
    global _CACHE_CONN, _CACHE_DB_URL
    _CACHE_DB_URL = db_url
    try:
        _CACHE_CONN = _fresh_conn(db_url)
    except Exception:
        _CACHE_CONN = None


def _archive_raw(endpoint: str, params: dict, response):
    """Best-effort write-through of a raw API page to enrichment_calls."""
    global _CACHE_CONN
    if _CACHE_DB_URL is None or response is None:
        return
    for attempt in (1, 2):
        try:
            if _CACHE_CONN is None:
                _CACHE_CONN = _fresh_conn(_CACHE_DB_URL)
            api_cache.put(_CACHE_CONN, "saleleads", endpoint, params, True, response)
            return
        except Exception:
            try:
                if _CACHE_CONN: _CACHE_CONN.close()
            except Exception:
                pass
            _CACHE_CONN = None  # force reconnect next attempt; swallow on final


_URN_LIKE_RE = re.compile(r"^[a-z0-9_-]{20,}$", re.I)  # ACoAA... / ACwAA... etc. are 28+ chars

def _slug_from_url(url: str) -> str:
    """Extract LinkedIn slug from a profile URL — case PRESERVED.

    Critical for URN-encoded slugs (`ACwAA…`, `ACoAA…`) which Saleleads /
    Fresh LinkedIn APIs treat as CASE-SENSITIVE. Lowercasing first
    silently breaks URN lookups (returns 0 posts). Vanity slugs are
    case-insensitive on LinkedIn but stored lowercase in URLs by convention,
    so they work either way."""
    if not url:
        return ""
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url, re.IGNORECASE)
    return m.group(1) if m else ""


def _looks_urn_slug(s: str) -> bool:
    """True for URN-encoded slugs like 'acwaacj5j0mbq-fj6swnvecp3brps_cxnqkpjs8'.
    These come from Apollo / legacy Fresh API ingests — Saleleads's /user/posts
    can't query them and needs a vanity slug instead."""
    if not s or "-" in s and len(s) < 20:
        return False
    return bool(_URN_LIKE_RE.match(s)) and (s.startswith(("acoaa", "acwaa", "acuaa", "acaaa")))


def vanity_from_urn_url(url: str, *, retries: int = 4,
                        backoff_base: int = 8) -> str | None:
    """When a profile URL is URN-encoded (no real vanity), use LeadMagic
    /profile-search to discover the real vanity slug. Returns the slug or None.

    Retries on HTTP 502 (LeadMagic's rate-limit signal — "Slow down and retry")
    and 429, with linear backoff. Returns None on any other failure or after
    `retries` exhausted.
    """
    key = os.environ.get("LEADMAGIC_API_KEY")
    if not key:
        return None
    d = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                "https://api.leadmagic.io/profile-search",
                data=json.dumps({"profile_url": url}).encode(),
                headers={"X-API-Key": key, "Content-Type": "application/json",
                         "User-Agent": "curl/7.88.1"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            break  # success
        except urllib.error.HTTPError as e:
            # 502 = "profile_search_rate_limited" per LeadMagic; 429 = standard rate-limit
            if e.code in (429, 502, 503) and attempt + 1 < retries:
                time.sleep(backoff_base * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt + 1 < retries:
                time.sleep(backoff_base * (attempt + 1))
                continue
            return None
    if d is None:
        return None
    # LeadMagic returns linkedin_url (vanity form) or public_identifier
    li = d.get("linkedin_url") or d.get("profile_url") or ""
    m = re.search(r"linkedin\.com/in/([^/?#]+)", (li or "").lower())
    if m and not _looks_urn_slug(m.group(1)):
        return m.group(1).rstrip("/")
    pub = (d.get("public_identifier") or "").lower().rstrip("/")
    if pub and not _looks_urn_slug(pub):
        return pub
    return None


def vanity_from_saleleads_search(name: str, *,
                                  headline_hint: str | None = None,
                                  company_hint: str | None = None) -> str | None:
    """Resolve a person to a LinkedIn vanity slug by name-searching Saleleads
    /api/v1/search/people, then scoring matches by name + headline/company hints.

    Different from `vanity_from_urn_url` (which uses LeadMagic): this one is
    free of LeadMagic rate-limits and works when our only input is a
    URN-encoded URL + the person's name (typical of Sales-Navigator-exported
    CSVs). One Saleleads call per person.

    Returns the vanity slug (without trailing slash) or None.

    Disambiguation rules:
      1. Filter to results whose normalized full_name == normalized input name.
      2. Score remaining matches by overlap with headline_hint + company_hint
         (the lead's stored headline / current_company).
      3. Pick the highest-scoring match; tie-break by Saleleads' default order.
      4. If no name-exact match exists, return None (avoid false positives).
    """
    if not name:
        return None
    res = saleleads_get("/api/v1/search/people", {"name": name, "page": 1})
    if not res or not res.get("success"):
        return None
    candidates = res.get("data") or []
    if not isinstance(candidates, list) or not candidates:
        return None

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    name_norm = _norm(name)
    exact_name = [p for p in candidates if _norm(p.get("full_name", "")) == name_norm]
    if not exact_name:
        return None  # too risky to pick a non-exact name match

    h_norm = _norm(headline_hint or "")
    c_norm = _norm(company_hint or "")
    h_tokens = {t for t in re.findall(r"[a-z0-9]+", (headline_hint or "").lower()) if len(t) >= 4}

    def score(p: dict) -> int:
        title = p.get("title") or p.get("headline") or ""
        title_norm = _norm(title)
        s = 0
        if c_norm and c_norm in title_norm:
            s += 3
        for t in h_tokens:
            if t in title_norm:
                s += 1
        return s

    exact_name.sort(key=score, reverse=True)
    best = exact_name[0]
    public_id = (best.get("public_identifier") or "").strip("/")
    if public_id and not _looks_urn_slug(public_id):
        return public_id
    return None


def _numeric_post_id(urn_or_id: str) -> str:
    """Extract numeric activity id from urn:li:activity:<n> or pass through digits."""
    if not urn_or_id:
        return ""
    m = re.search(r"(\d{10,})", urn_or_id)
    return m.group(1) if m else urn_or_id


# ── Rate-limit-aware HTTP ─────────────────────────────────────────────────────

class SaleleadsCreditExhausted(Exception):
    """Raised when Saleleads/RapidAPI returns HTTP 429 'Request denied' with
    cost>=1 too many times in a row (no successful call in between).

    That signature is ambiguous on its own:
      (a) per-minute rate-limit on the shared plan (RECOVERABLE — back off
          and retry; the plan has ~120 req/min shared across multiple
          processes, so bursts trigger this)
      (b) plan-quota exhausted (NOT recoverable without intervention)

    We treat the first N occurrences as (a) and back off; only after
    SALELEADS_MAX_CONSECUTIVE_DENIED in a row do we assume (b) and raise.
    """


# ── Shared-plan throttle (process-local) ──────────────────────────────────────
#
# The Saleleads plan is 120 req/min shared across multiple processes/users.
# Self-throttle so this script's share stays well under the cap and we don't
# hammer the cap on bursts. Defaults to 1.0s min interval = ≤60/min from us.

import threading
_throttle_lock = threading.Lock()
_last_call_at: float = 0.0
_consecutive_denied: int = 0

# Tunables — overridable via env.
SALELEADS_MIN_INTERVAL_S = float(os.environ.get("SALELEADS_MIN_INTERVAL_S", "1.0"))
# Wait this many seconds after a "Request denied" before retrying. Scales
# with consecutive failures so the bucket has time to refill.
SALELEADS_DENIED_BACKOFF_S = float(os.environ.get("SALELEADS_DENIED_BACKOFF_S", "30.0"))
# After this many consecutive "Request denied" + cost:1 responses with NO
# success in between, give up — at this point it's quota-exhausted, not rate.
SALELEADS_MAX_CONSECUTIVE_DENIED = int(os.environ.get("SALELEADS_MAX_CONSECUTIVE_DENIED", "5"))

# ── Cost accounting (process-local, advisory) ─────────────────────────────────
#
# Tracks cumulative `cost` field returned by Saleleads across calls. Used by
# higher-level orchestrators to (a) log running totals, (b) decide whether to
# pause a long-running batch that's burning quota faster than expected.

_cost_lock = threading.Lock()
_cost_charged: int = 0
_cost_free: int = 0
_calls_total: int = 0
_calls_success: int = 0
_calls_charged_denial: int = 0


def saleleads_cost_snapshot() -> dict:
    """Return a snapshot of cumulative Saleleads usage in this process."""
    with _cost_lock:
        return {
            "calls_total":           _calls_total,
            "calls_success":         _calls_success,
            "calls_charged_denial":  _calls_charged_denial,
            "cost_charged":          _cost_charged,
            "cost_free":             _cost_free,
        }


def _bump_cost(d: dict, charged_denial: bool = False, success: bool = False):
    """Internal — increment cost counters from a parsed response body."""
    global _cost_charged, _cost_free, _calls_total, _calls_success, _calls_charged_denial
    with _cost_lock:
        _calls_total += 1
        try:
            c = int((d or {}).get("cost") or 0)
        except (TypeError, ValueError):
            c = 0
        if c >= 1:
            _cost_charged += c
        else:
            _cost_free += 1
        if charged_denial:
            _calls_charged_denial += 1
        if success:
            _calls_success += 1


def _pace_throttle():
    """Enforce a minimum gap between Saleleads calls so we stay well under
    the shared plan's per-minute cap on bursts."""
    global _last_call_at
    with _throttle_lock:
        gap = time.monotonic() - _last_call_at
        if gap < SALELEADS_MIN_INTERVAL_S:
            time.sleep(SALELEADS_MIN_INTERVAL_S - gap)
        _last_call_at = time.monotonic()


def saleleads_get(path: str, params: dict, retries: int = 6, backoff_base: int = 15) -> dict | None:
    """GET with retry-on-429 (upstream LinkedIn scrape throttle). Returns None on hard failure.

    User-Agent must look like curl — Cloudflare WAF blocks default Python-urllib
    with error code 1010 (HTTP 403). Matches the workaround used by the Apollo
    skill and the Saleleads CLI.

    Raises SaleleadsCreditExhausted on the "Request denied + cost>=1" signature
    (RapidAPI charging us for denied calls), so the caller can stop the run
    instead of burning more credits on retries.
    """
    qs = urllib.parse.urlencode(params)
    url = f"{SL_BASE}{path}?{qs}"
    headers = {
        "x-rapidapi-host": SL_HOST,
        "x-rapidapi-key": _api_key(),
        "User-Agent": "curl/7.88.1",
        "Accept": "application/json",
    }
    global _consecutive_denied
    last_err = None
    for attempt in range(retries):
        _pace_throttle()
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read()
                d = json.loads(raw)
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            try: body = e.read().decode()[:300]
            except: body = ""
            # Distinguish two flavours of 429:
            #   (a) "Request denied" + cost>=1 → CHARGED denial. Could be the
            #       shared plan's per-minute rate-limit (recoverable: back off
            #       long enough that the rolling-window bucket refills) OR a
            #       hard plan-quota exhaustion (not recoverable). We can't
            #       tell from a single response, so retry with a long sleep;
            #       only bail if we hit MAX_CONSECUTIVE_DENIED in a row with
            #       no success in between.
            #   (b) Other 429 / 5xx → standard transient; short backoff.
            parsed_body = None
            if e.code == 429:
                try:
                    parsed_body = json.loads(body)
                    is_charged_denial = (parsed_body.get("success") is False
                                         and "denied" in (parsed_body.get("message") or "").lower()
                                         and int(parsed_body.get("cost") or 0) >= 1)
                except Exception:
                    is_charged_denial = False
                # Account for the cost of this attempt before we decide what to do.
                _bump_cost(parsed_body or {}, charged_denial=is_charged_denial)
                if is_charged_denial:
                    _consecutive_denied += 1
                    if _consecutive_denied > SALELEADS_MAX_CONSECUTIVE_DENIED:
                        raise SaleleadsCreditExhausted(
                            f"Saleleads HTTP 429 'Request denied' with cost>=1 "
                            f"{_consecutive_denied} times in a row — plan quota "
                            f"likely exhausted (not just rate-limited). Body: {body[:200]}"
                        )
                    sleep_s = SALELEADS_DENIED_BACKOFF_S * _consecutive_denied
                    print(f"    · 429 'Request denied' #{_consecutive_denied} — backing off {sleep_s:.0f}s "
                          f"(shared plan likely rate-limited)")
                    time.sleep(sleep_s)
                    continue
            # Standard retry path for non-charged 429 + 5xx transients.
            if e.code == 429 or 500 <= e.code < 600:
                time.sleep(backoff_base * (attempt + 1))
                continue
            print(f"    ! HTTP {e.code} {path}: {body}")
            return None
        except Exception as e:
            last_err = e
            time.sleep(backoff_base * (attempt + 1))
            continue
        if d.get("success") is True:
            _consecutive_denied = 0  # any success resets the streak
            _bump_cost(d, success=True)
            return d
        # Account for any non-success response too.
        _bump_cost(d)
        msg = (d.get("message") or "").lower()
        # Soft Saleleads failures: "undefined undefined" with cost:0 means the upstream
        # LinkedIn scrape failed; we're not charged. Retry.
        if "429" in msg or "rate" in msg or "denied" in msg or "undefined" in msg:
            time.sleep(backoff_base * (attempt + 1))
            last_err = msg
            continue
        return d  # hard logical failure (ZodError, 404 → caller decides what to do)
    print(f"    ! saleleads_get FAILED after {retries} retries: {last_err}")
    return None


# ── DB helpers ────────────────────────────────────────────────────────────────

def target_companies(cur, include_self: bool, company_filter: str | None) -> list[dict]:
    """Return all flagged companies (competitor + optionally self) for engager research."""
    rels = ["competitor"] + (["self"] if include_self else [])
    cur.execute("""
        SELECT c.id, c.linkedin_slug, c.linkedin_url, c.name, c.saleleads_id, cr.relationship
        FROM companies c
        JOIN company_relationships cr ON cr.company_id = c.id
        WHERE cr.relationship = ANY(%s)
        ORDER BY cr.relationship, c.name
    """, (rels,))
    rows = [{"id": r[0], "linkedin_slug": r[1], "linkedin_url": r[2], "name": r[3],
             "saleleads_id": r[4], "relationship": r[5]} for r in cur.fetchall()]
    if company_filter:
        f = company_filter.lower()
        rows = [c for c in rows if f in (c["linkedin_slug"] or "").lower()
                or f in (c["name"] or "").lower()
                or f == (c["saleleads_id"] or "")]
    return rows


def company_execs(cur, company_name: str, relationship: str,
                  exclude_tag: str | None = None) -> list[dict]:
    """Return all execs we've ingested for this company (via find_senior_execs).
    If `exclude_tag` is set, leads carrying that tag are dropped — use it to hand
    a person off to another pipeline (e.g. exclude `buyer_monitor_target` so a
    concurrent buyer-monitor sweep owns the overlap and we don't double-pull)."""
    cur.execute("""
        SELECT DISTINCT l.id, l.linkedin_url, l.linkedin_urn, l.public_id, l.name, l.current_title
        FROM lead_sources ls
        JOIN leads l ON l.id = ls.lead_id
        WHERE ls.source_type = 'find_senior_execs'
          AND ls.source_label = %s
          AND (%s IS NULL OR NOT EXISTS (
                SELECT 1 FROM lead_tags lt WHERE lt.lead_id = l.id AND lt.tag = %s))
    """, (f"{company_name} ({relationship})", exclude_tag, exclude_tag))
    return [{"id": r[0], "linkedin_url": r[1], "linkedin_urn": r[2], "public_id": r[3],
             "name": r[4], "current_title": r[5]} for r in cur.fetchall()]


def company_curated_posters(cur, company_name: str, relationship: str,
                            company_url: str) -> list[dict]:
    """Return the CURATED post-author set for this company: leads we deliberately
    chose to monitor — i.e. either
      (A) ingested as a senior exec for this company (find_senior_execs), OR
      (B) tagged `buyer_monitor_target` AND currently at this company.
    Used by `--posters curated`. Deliberately does NOT include arbitrary leads who
    merely happen to sit at a competitor (e.g. harvested reactors) — only the
    senior + hand-picked-monitor people."""
    label = f"{company_name} ({relationship})"
    cur.execute("""
        SELECT DISTINCT l.id, l.linkedin_url, l.linkedin_urn, l.public_id, l.name, l.current_title
        FROM leads l
        WHERE l.linkedin_url IS NOT NULL
          AND (
                EXISTS (SELECT 1 FROM lead_sources ls
                         WHERE ls.lead_id = l.id
                           AND ls.source_type = 'find_senior_execs'
                           AND ls.source_label = %s)
             OR (
                EXISTS (SELECT 1 FROM lead_tags lt
                         WHERE lt.lead_id = l.id
                           AND lt.tag = 'buyer_monitor_target')
                AND regexp_replace(lower(l.current_company_url), '/+$', '')
                  = regexp_replace(lower(%s),                    '/+$', '')
             )
          )
    """, (label, company_url))
    return [{"id": r[0], "linkedin_url": r[1], "linkedin_urn": r[2], "public_id": r[3],
             "name": r[4], "current_title": r[5]} for r in cur.fetchall()]


def recent_search_exists(cur, query: str, max_age_days: int) -> bool:
    cur.execute("""
        SELECT 1 FROM searches
        WHERE skill = %s AND query = %s
          AND ran_at > now() - (%s || ' days')::interval
        LIMIT 1
    """, (SKILL_NAME, query, str(max_age_days)))
    return cur.fetchone() is not None


def insert_search(cur, query: str, params: dict, total_posts: int) -> str:
    cur.execute("""
        INSERT INTO searches (skill, query, params, total_posts_returned)
        VALUES (%s, %s, %s::jsonb, %s)
        RETURNING id
    """, (SKILL_NAME, query, json.dumps(params), total_posts))
    return cur.fetchone()[0]


def upsert_post(cur, p: dict, poster_name: str, poster_url: str) -> tuple[str, str]:
    """UPSERT a post row. Returns (post_uuid, numeric_post_id)."""
    urn = p.get("urn") or p.get("id") or p.get("activity_urn")
    if not urn:
        return "", ""
    num_id = _numeric_post_id(urn)
    post_urn = f"urn:li:activity:{num_id}" if num_id else urn
    text = (p.get("text") or p.get("post_text") or p.get("commentary") or "")[:8000]
    # Saleleads nests engagement counts under `activity` (num_likes,
    # num_comments, num_shares). Top-level keys are absent. Fall back to
    # top-level for forward-compat with other shapes.
    act = p.get("activity") or {}
    likes = (act.get("num_likes") if isinstance(act, dict) else None) or \
            p.get("reactions") or p.get("likes") or p.get("num_likes") or \
            p.get("total_reactions") or 0
    comments_n = (act.get("num_comments") if isinstance(act, dict) else None) or \
                 p.get("comments") or p.get("comments_count") or p.get("num_comments") or 0
    shares = (act.get("num_shares") if isinstance(act, dict) else None) or p.get("shares") or 0
    if isinstance(likes, dict): likes = likes.get("total", 0)
    posted_at = p.get("posted_at") or p.get("date") or p.get("created_at")
    cur.execute("""
        INSERT INTO posts (post_urn, post_url, poster_name, poster_linkedin_url, posted_at,
                           post_text, likes, comments_count, shares, post_type, raw_data, last_scraped_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
        ON CONFLICT (post_urn) DO UPDATE SET
            post_url        = COALESCE(EXCLUDED.post_url,        posts.post_url),
            poster_name     = COALESCE(EXCLUDED.poster_name,     posts.poster_name),
            poster_linkedin_url = COALESCE(EXCLUDED.poster_linkedin_url, posts.poster_linkedin_url),
            posted_at       = COALESCE(EXCLUDED.posted_at,       posts.posted_at),
            post_text       = COALESCE(NULLIF(EXCLUDED.post_text,''), posts.post_text),
            likes           = GREATEST(EXCLUDED.likes,           posts.likes),
            comments_count  = GREATEST(EXCLUDED.comments_count,  posts.comments_count),
            shares          = COALESCE(EXCLUDED.shares,          posts.shares),
            post_type       = COALESCE(EXCLUDED.post_type,       posts.post_type),
            raw_data        = EXCLUDED.raw_data,
            last_scraped_at = now()
        RETURNING id
    """, (post_urn, p.get("url") or p.get("post_url") or "",
          poster_name, poster_url, posted_at,
          text, int(likes) if str(likes).isdigit() else None,
          int(comments_n) if str(comments_n).isdigit() else None,
          int(shares) if str(shares).isdigit() else None,
          p.get("post_type") or p.get("type"),
          json.dumps(p)))
    return cur.fetchone()[0], num_id


def upsert_lead_from_engagement(cur, person: dict) -> str | None:
    """UPSERT a lead from an engagement payload. Returns lead UUID or None if no LinkedIn URL."""
    url = (person.get("url") or person.get("profile_url") or person.get("linkedin_url")
           or person.get("profileUrl") or "")
    if not url:
        return None
    li_url = resolve_canonical_url(cur, normalize_linkedin_url(url),
                                   urn_hint=person.get("urn"))
    name = (person.get("name") or person.get("full_name")
            or f"{person.get('first_name','')} {person.get('last_name','')}".strip()
            or person.get("title", "")[:80] or "").strip()
    headline = person.get("headline") or person.get("title") or person.get("subtitle") or ""
    # public_id is the vanity handle. Only fall back to the URL slug when it's a
    # genuine vanity slug — never an URN-form token (ACoAA…/ACwAA…), which would
    # poison the public_id-based identity fallback. (The trigger also captures it.)
    pubid = person.get("public_id")
    if not pubid:
        slug = _slug_from_url(li_url)
        pubid = slug if (slug and not member_urn_token(slug)) else None
    cur.execute("""
        INSERT INTO leads (linkedin_url, linkedin_urn, public_id, name, headline)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (linkedin_url) DO UPDATE SET
            linkedin_urn = COALESCE(EXCLUDED.linkedin_urn,   leads.linkedin_urn),
            public_id    = COALESCE(EXCLUDED.public_id,      leads.public_id),
            name         = COALESCE(leads.name,              EXCLUDED.name),
            headline     = COALESCE(EXCLUDED.headline,       leads.headline),
            updated_at   = now()
        RETURNING id
    """, (li_url, person.get("urn"), pubid, name or None, headline or None))
    return cur.fetchone()[0]


def insert_engagement(cur, post_uuid: str, lead_uuid: str, etype: str,
                      reaction_type: str | None, comment_text: str | None, raw: dict):
    cur.execute("""
        INSERT INTO post_engagements (post_id, lead_id, engagement_type, reaction_type, comment_text, raw_data)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
    """, (post_uuid, lead_uuid, etype, reaction_type, comment_text, json.dumps(raw)))


def insert_lead_source_engager(cur, lead_uuid: str, source_label: str, raw: dict):
    cur.execute("""
        INSERT INTO lead_sources (lead_id, source_type, source_label, source_date, raw_data)
        VALUES (%s, 'engagers_research', %s, now()::date, %s::jsonb)
    """, (lead_uuid, source_label, json.dumps(raw)))


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def fetch_user_posts(username: str, max_pages: int = 10) -> list[dict]:
    out, page = [], 1
    while page <= max_pages:
        d = saleleads_get("/api/v1/user/posts", {"username": username, "page": page})
        _archive_raw("/api/v1/user/posts", {"username": username, "page": page}, d)
        if not d or not d.get("success"):
            break
        data = d.get("data") or d.get("posts") or []
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < 10:
            break
        page += 1
    return out


def fetch_company_posts(company_id: str, max_pages: int = 3) -> list[dict]:
    out, page = [], 1
    while page <= max_pages:
        d = saleleads_get("/api/v1/company/posts", {"company_id": company_id, "page": page})
        _archive_raw("/api/v1/company/posts", {"company_id": company_id, "page": page}, d)
        if not d or not d.get("success"):
            break
        data = d.get("data") or d.get("posts") or []
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < 10:
            break
        page += 1
    return out


def fetch_post_reactions(post_id: str, max_pages: int = 50) -> list[dict]:
    """Paginate through all reactions on a post. Raised cap from 5 → 50
    pages so we capture every reactor on high-engagement posts (was
    artificially limiting us to ~250 reactions/post)."""
    out, page = [], 1
    while page <= max_pages:
        d = saleleads_get("/api/v1/post/reactions", {"post_id": post_id, "page": page})
        _archive_raw("/api/v1/post/reactions", {"post_id": post_id, "page": page}, d)
        if not d or not d.get("success"):
            break
        data = d.get("data") or d.get("reactions") or []
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < 10:
            break
        page += 1
    return out


def fetch_post_comments(post_id: str, max_pages: int = 50) -> list[dict]:
    """Same as fetch_post_reactions — was capped at 5 pages."""
    out, page = [], 1
    while page <= max_pages:
        d = saleleads_get("/api/v1/post/comments", {"post_id": post_id, "page": page})
        _archive_raw("/api/v1/post/comments", {"post_id": post_id, "page": page}, d)
        if not d or not d.get("success"):
            break
        data = d.get("data") or d.get("comments") or []
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < 10:
            break
        page += 1
    return out


# ── Pipeline per poster ──────────────────────────────────────────────────────

def harvest_posts_for(db_url: str, poster_kind: str, posts: list[dict], poster_name: str,
                      poster_url: str, source_label: str, max_engagers: int,
                      include_reactors: bool, include_commenters: bool) -> dict:
    """Persist a batch of posts + their engagement. Opens a fresh per-post DB
    connection so we never hold one across long Saleleads /post/reactions and
    /post/comments waits."""
    stats = {"posts": 0, "reactions": 0, "comments": 0, "new_leads": 0}
    for p in posts:
        try:
            # Upsert the post first (short transaction, reconnect-retried).
            conn = _fresh_conn(db_url)
            try:
                post_uuid = num_id = None
                for attempt in (1, 2):
                    try:
                        with conn.cursor() as cur:
                            post_uuid, num_id = upsert_post(cur, p, poster_name, poster_url)
                        conn.commit()
                        stats["posts"] += 1
                        break
                    except Exception:
                        try: conn.rollback()
                        except: pass
                        if attempt == 1:
                            conn = _ensure_conn(conn, db_url)
                        else:
                            raise
            finally:
                try: conn.close()
                except: pass

            if not num_id:
                continue

            # Reactions — fetched without a DB conn open; then opened per batch.
            if include_reactors:
                reactions = fetch_post_reactions(num_id)[:max_engagers]
                if reactions:
                    conn = _fresh_conn(db_url)
                    try:
                        for r in reactions:
                            person = r.get("user") or r.get("actor") or r
                            rtype = r.get("reaction_type") or r.get("type")
                            # Reconnect + retry each row once so a single dropped
                            # connection mid-batch can't cascade across the rest.
                            for attempt in (1, 2):
                                try:
                                    with conn.cursor() as cur:
                                        lead_uuid = upsert_lead_from_engagement(cur, person)
                                        if lead_uuid:
                                            insert_engagement(cur, post_uuid, lead_uuid, "reaction", rtype, None, r)
                                            insert_lead_source_engager(cur, lead_uuid, source_label, r)
                                            stats["reactions"] += 1
                                    conn.commit()
                                    break
                                except Exception as e:
                                    try: conn.rollback()
                                    except: pass
                                    if attempt == 1:
                                        conn = _ensure_conn(conn, db_url)
                                    else:
                                        print(f"      ! reaction insert failed: {type(e).__name__}: {str(e)[:120]}")
                    finally:
                        try: conn.close()
                        except: pass

            # Comments — same pattern.
            if include_commenters:
                comments = fetch_post_comments(num_id)[:max_engagers]
                if comments:
                    conn = _fresh_conn(db_url)
                    try:
                        for cm in comments:
                            person = cm.get("user") or cm.get("actor") or cm
                            ctext = cm.get("text") or cm.get("comment_text") or cm.get("body") or ""
                            for attempt in (1, 2):
                                try:
                                    with conn.cursor() as cur:
                                        lead_uuid = upsert_lead_from_engagement(cur, person)
                                        if lead_uuid:
                                            insert_engagement(cur, post_uuid, lead_uuid, "comment", None, ctext[:4000], cm)
                                            insert_lead_source_engager(cur, lead_uuid, source_label, cm)
                                            stats["comments"] += 1
                                    conn.commit()
                                    break
                                except Exception as e:
                                    try: conn.rollback()
                                    except: pass
                                    if attempt == 1:
                                        conn = _ensure_conn(conn, db_url)
                                    else:
                                        print(f"      ! comment insert failed: {type(e).__name__}: {str(e)[:120]}")
                    finally:
                        try: conn.close()
                        except: pass
        except Exception as e:
            # Transient failure on this post (e.g. Neon unreachable). Skip it and
            # keep the run alive; a later --refresh re-pulls it cleanly.
            print(f"      ! skipping post after error: {type(e).__name__}: {str(e)[:120]}")
            continue
    return stats


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _fresh_conn(db_url: str):
    """Open a new psycopg2 connection with TCP keepalives to survive long
    Saleleads rate-limit waits (Neon scales to zero and will drop idle conns)."""
    return psycopg2.connect(
        db_url,
        connect_timeout=20,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
    )


def _ensure_conn(conn, db_url: str):
    """Always return a fresh, live connection. Cheap (~500ms on Neon) and
    sidesteps the entire class of 'probe passed but next op fails' SSL drops.
    Closes the old conn so we don't leak slots."""
    try:
        if conn: conn.close()
    except Exception:
        pass
    return _fresh_conn(db_url)


def run(client: str, company_filter: str | None, include_self: bool,
        max_posts_per_exec: int, max_engagers_per_post: int,
        include_reactors: bool, include_commenters: bool,
        max_age_days: int, refresh: bool, posters: str = "execs",
        exclude_tag: str | None = None) -> int:
    load_client_env(client)
    db_url = database_url(client)
    _init_archive(db_url)  # write-through raw archive → enrichment_calls

    # Phase 1 — connection #1: list target companies, close immediately.
    with _fresh_conn(db_url) as conn:
        with conn.cursor() as cur:
            companies = target_companies(cur, include_self, company_filter)
    print(f"=== {client}: harvesting engagers for {len(companies)} flagged companies ===")
    for c in companies:
        print(f"  · {c['relationship']:<10} {c['name']} ({c['linkedin_slug']})")

    grand_totals = {"posts": 0, "reactions": 0, "comments": 0}

    # Phase 2 — open a fresh connection per company.
    for c in companies:
        company_label = f"{c['name']} ({c['relationship']})"
        print(f"\n--- {company_label} ---")
        try:
            conn = _fresh_conn(db_url)
        except Exception as e:
            print(f"  ! DB connection failed: {e}; skipping this company")
            continue
        try:
            # 1) Get the post-authors for this company.
            #    posters='execs'   → only find_senior_execs leads (default, cheapest).
            #    posters='curated' → senior execs ∪ buyer_monitor_target leads at this
            #                        company (the deliberately-monitored set).
            with conn.cursor() as cur:
                if posters == "curated":
                    execs = company_curated_posters(cur, c["name"], c["relationship"],
                                                    c["linkedin_url"])
                else:
                    execs = company_execs(cur, c["name"], c["relationship"],
                                          exclude_tag=exclude_tag)
            kind = "curated post-authors" if posters == "curated" else "execs in lead_sources"
            print(f"  {len(execs)} {kind} for {company_label!r}")

            # 2) Per-exec posts + engagement
            for ex in execs:
                raw_slug = _slug_from_url(ex["linkedin_url"]) or ex["public_id"] or ""
                vanity = raw_slug
                if _looks_urn_slug(raw_slug):
                    resolved = vanity_from_urn_url(ex["linkedin_url"])
                    if resolved:
                        print(f"    · {ex['name']:<28} URN→vanity via LeadMagic: {raw_slug[:12]}…→{resolved}")
                        vanity = resolved
                    else:
                        print(f"    skip {ex['name']!r}: URN-only slug ({raw_slug[:20]}…), LeadMagic couldn't resolve")
                        continue
                if not vanity:
                    print(f"    skip {ex['name']!r}: no LinkedIn vanity slug")
                    continue
                search_key = f"user_posts:{vanity}"
                conn = _ensure_conn(conn, db_url)
                with conn.cursor() as cur:
                    if not refresh and recent_search_exists(cur, search_key, max_age_days):
                        print(f"    · {ex['name']:<28} skipped (recent search for {search_key!r})")
                        continue
                print(f"    → {ex['name']:<28} fetching /user/posts?username={vanity}")
                posts = fetch_user_posts(vanity)[:max_posts_per_exec]
                conn = _ensure_conn(conn, db_url)
                stats = harvest_posts_for(db_url, "exec", posts, ex["name"] or "",
                                           ex["linkedin_url"] or "", company_label,
                                           max_engagers_per_post,
                                           include_reactors, include_commenters)
                # Only record the `searches` row if we actually got posts. A 0-post
                # response is ambiguous (genuine 0 vs Saleleads upstream failure),
                # and recording it would cause future runs to skip the exec without
                # --refresh. Better to retry on next run.
                if stats["posts"] > 0:
                    conn = _ensure_conn(conn, db_url)
                    with conn.cursor() as cur:
                        insert_search(cur, search_key,
                                      {"company": c["name"], "exec": ex["name"], "vanity": vanity,
                                       "max_posts": max_posts_per_exec, "max_engagers": max_engagers_per_post},
                                      stats["posts"])
                    conn.commit()
                    print(f"      ✓ {stats['posts']} posts, {stats['reactions']} reactions, "
                          f"{stats['comments']} comments")
                else:
                    print(f"      · 0 posts returned (not recorded as search — will retry on next run)")
                for k in grand_totals: grand_totals[k] += stats[k]

            # 3) Company-page posts
            if c["saleleads_id"]:
                search_key = f"company_posts:{c['saleleads_id']}"
                conn = _ensure_conn(conn, db_url)
                with conn.cursor() as cur:
                    skip = (not refresh) and recent_search_exists(cur, search_key, max_age_days)
                if skip:
                    print(f"  · company-page posts skipped (recent search for {search_key!r})")
                else:
                    print(f"  → company-page fetching /company/posts?company_id={c['saleleads_id']}")
                    posts = fetch_company_posts(c["saleleads_id"])[:max_posts_per_exec * 2]
                    conn = _ensure_conn(conn, db_url)
                    stats = harvest_posts_for(db_url, "company", posts, c["name"] or "",
                                               c["linkedin_url"] or "", company_label,
                                               max_engagers_per_post,
                                               include_reactors, include_commenters)
                    conn = _ensure_conn(conn, db_url)
                    with conn.cursor() as cur:
                        insert_search(cur, search_key,
                                      {"company": c["name"], "saleleads_id": c["saleleads_id"],
                                       "max_posts": max_posts_per_exec * 2,
                                       "max_engagers": max_engagers_per_post},
                                      stats["posts"])
                    conn.commit()
                    print(f"    ✓ {stats['posts']} posts, {stats['reactions']} reactions, "
                          f"{stats['comments']} comments")
                    for k in grand_totals: grand_totals[k] += stats[k]
        except Exception as e:
            print(f"  ! error processing {company_label!r}: {type(e).__name__}: {e}")
        finally:
            try: conn.close()
            except: pass

    print(f"\n=== done — totals: "
          f"{grand_totals['posts']} posts, {grand_totals['reactions']} reactions, "
          f"{grand_totals['comments']} comments ===")
    return 0


def main():
    ap = argparse.ArgumentParser(description="DB-only engagers research for competitors + self in a client's MarketBase.")
    ap.add_argument("--client", required=True)
    ap.add_argument("--company", help="Limit to one company (matches slug, name, or saleleads_id).")
    ap.add_argument("--no-self", action="store_true", help="Exclude relationship='self' companies (default includes them).")
    ap.add_argument("--max-posts-per-exec", type=int, default=20)
    ap.add_argument("--max-engagers-per-post", type=int, default=500)
    ap.add_argument("--no-reactors", action="store_true")
    ap.add_argument("--no-commenters", action="store_true")
    ap.add_argument("--max-age-days", type=int, default=14,
                    help="Skip a (person|company)-posts pull if a `searches` row younger than this exists. Default 14.")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore --max-age-days and re-pull everything.")
    ap.add_argument("--posters", choices=["execs", "curated"], default="execs",
                    help="Whose posts to scrape for engagers. 'execs' (default): only "
                         "find_senior_execs leads + company page. 'curated': senior execs "
                         "PLUS buyer_monitor_target-tagged leads at the company + company "
                         "page (the deliberately-monitored set; excludes arbitrary leads "
                         "who merely sit at a competitor). Wider coverage, higher API cost.")
    ap.add_argument("--exclude-tag",
                    help="Drop any post-author carrying this tag (e.g. "
                         "'buyer_monitor_target' to hand the overlap to a concurrent "
                         "buyer-monitor sweep and avoid double-pulling the same person).")
    args = ap.parse_args()
    sys.exit(run(args.client, args.company, not args.no_self,
                 args.max_posts_per_exec, args.max_engagers_per_post,
                 not args.no_reactors, not args.no_commenters,
                 args.max_age_days, args.refresh, posters=args.posters,
                 exclude_tag=args.exclude_tag))


if __name__ == "__main__":
    main()
