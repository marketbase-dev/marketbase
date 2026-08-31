#!/usr/bin/env python3
"""post_impressions_report.py — owner-analytics post report for a client's MarketBase.

For one LinkedIn person whose OWN account is connected to Unipile, this:

  1. Fetches their latest N posts via Saleleads (`/api/v1/user/posts`) and
     UPSERTs each into the client's MarketBase `posts` table (reusing the battle-
     tested `upsert_post` — so post_urn / text / likes / comments / shares /
     raw_data all land exactly like the rest of MarketBase).
  2. Pulls the SAME posts back through Unipile as the owning account
     (`/api/v1/users/<provider_id>/posts`), which is the only path that
     exposes `impressions_counter` — LinkedIn shows reach/views to the post
     OWNER only. Third-party scrapers (Saleleads, LeadMagic) never see it.
  3. Writes impressions into `posts.impressions` (+ `analytics_source='unipile'`,
     `analytics_at=now()`). Reposts are SKIPPED — their reach belongs to the
     original author, so we leave their impressions NULL.
  4. Emits the originals (non-reposts) ranked by impressions — as a table on
     stdout, or as JSON (`--json`) so the calling agent can title each post
     from its content and render the final markdown table.

This is the legitimate owner-analytics path, NOT scraping: we read the
account's own analytics through its own connected Unipile session.

Prereqs:
  • ~/.env.<Client> with GTM_DB_CONNSTRING (the client's Neon DB).
  • ~/.env with FRESH_LINKEDIN_DATA_API_KEY (Saleleads), UNIPILE_BASE_URL,
    UNIPILE_API_KEY.
  • The target person's OWN LinkedIn connected to Unipile (else no impressions).

Usage:
  python3 post_impressions_report.py --client Acme-AI \
      --profile https://www.linkedin.com/in/alonrosenberg --limit 20
  python3 post_impressions_report.py --client Acme-AI \
      --profile alonrosenberg --json            # machine-readable for titling
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

from lib import load_client_env, database_url, apply_schema
from engagers_research import fetch_user_posts, upsert_post, _fresh_conn


def slug_from_profile(profile: str) -> str:
    """Accept a full /in/<slug> URL or a bare vanity slug; return the slug."""
    m = re.search(r"linkedin\.com/in/([^/?#]+)", profile, re.I)
    return (m.group(1) if m else profile).strip().strip("/")


def numeric_id(s: str) -> str:
    m = re.search(r"(\d{10,})", s or "")
    return m.group(1) if m else ""


# ── Unipile ───────────────────────────────────────────────────────────────────

def _uni_get(path: str, params: dict) -> dict:
    base = os.environ["UNIPILE_BASE_URL"].rstrip("/")
    key = os.environ["UNIPILE_API_KEY"]
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-API-KEY": key, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def resolve_unipile_account(full_name: str, explicit_id: str | None) -> str:
    """Return the Unipile account id of the owning LinkedIn account.

    If --unipile-account-id was passed, trust it. Otherwise match a connected
    LINKEDIN account whose display name equals the person's full name.
    """
    if explicit_id:
        return explicit_id
    data = _uni_get("/api/v1/accounts", {"limit": 200})
    accounts = data.get("items") or data or []
    want = (full_name or "").strip().lower()
    for a in accounts:
        if (a.get("type") or "").upper() == "LINKEDIN" and (a.get("name") or "").strip().lower() == want:
            return a["id"]
    raise SystemExit(
        f"No connected Unipile LINKEDIN account matches '{full_name}'. "
        f"Pass --unipile-account-id explicitly, or connect the person's own "
        f"LinkedIn to Unipile (owner analytics require their own session)."
    )


def fetch_unipile_analytics(provider_id: str, account_id: str, need: int) -> dict[str, dict]:
    """Map numeric_post_id -> Unipile post item (carries impressions_counter,
    is_repost, reaction/comment/repost counters). Paginates until we have at
    least `need` items or run out."""
    out: dict[str, dict] = {}
    cursor = None
    for _ in range(10):  # hard cap on pages
        params = {"account_id": account_id, "limit": 50}
        if cursor:
            params["cursor"] = cursor
        d = _uni_get(f"/api/v1/users/{provider_id}/posts", params)
        for x in d.get("items", []):
            out[numeric_id(x.get("social_id", ""))] = x
        cursor = d.get("cursor")
        if not cursor or len(out) >= need + 10:
            break
    return out


# ── Main ────────────────────────────────────────────────────────────────────

def run(client: str, profile: str, limit: int, unipile_account_id: str | None,
        as_json: bool) -> int:
    load_client_env(client)
    # Ensure the impressions columns exist (migration 025). Idempotent.
    apply_schema(client)
    db_url = database_url(client)
    vanity = slug_from_profile(profile)

    # 1) Saleleads → posts rows.
    pages = max(1, (limit + 9) // 10)
    sl_posts = fetch_user_posts(vanity, max_pages=pages)[:limit]
    if not sl_posts:
        sys.exit(f"Saleleads returned no posts for '{vanity}'. Check the slug / API key.")
    author = sl_posts[0].get("author") or {}
    full_name = author.get("full_name") or vanity
    poster_url = author.get("url") or f"https://www.linkedin.com/in/{vanity}"
    provider_id = author.get("urn")  # ACoAA… — the id Unipile's users endpoint wants
    if not provider_id:
        sys.exit("Saleleads post author had no `urn`; cannot address Unipile users endpoint.")

    saved: dict[str, str] = {}  # numeric_id -> post_urn
    with _fresh_conn(db_url) as conn:
        with conn.cursor() as cur:
            for p in sl_posts:
                puid, nid = upsert_post(cur, p, full_name, poster_url)
                if puid and nid:
                    saved[nid] = f"urn:li:activity:{nid}"
        conn.commit()
    print(f"· Saleleads: upserted {len(saved)} posts for {full_name} into {client} MarketBase", file=sys.stderr)

    # 2) Unipile → owner impressions.
    acct = resolve_unipile_account(full_name, unipile_account_id)
    amap = fetch_unipile_analytics(provider_id, acct, need=len(saved))

    # 3) Write impressions for originals; skip reposts.
    rows = []
    reposts = unmatched = 0
    with _fresh_conn(db_url) as conn:
        with conn.cursor() as cur:
            for nid, urn in saved.items():
                x = amap.get(nid)
                if not x:
                    unmatched += 1
                    continue
                if x.get("is_repost"):
                    reposts += 1
                    continue
                imp = x.get("impressions_counter") or 0
                cur.execute(
                    "UPDATE posts SET impressions=%s, analytics_source='unipile', "
                    "analytics_at=now() WHERE post_urn=%s", (imp, urn))
                rows.append({
                    "impressions": imp,
                    "reactions": x.get("reaction_counter") or 0,
                    "comments": x.get("comment_counter") or 0,
                    "shares": x.get("repost_counter") or 0,
                    "url": x.get("share_url") or f"https://www.linkedin.com/feed/update/{urn}",
                    "text": x.get("text") or "",
                })
        conn.commit()
    rows.sort(key=lambda r: -r["impressions"])
    print(f"· Unipile: wrote impressions for {len(rows)} originals "
          f"(skipped {reposts} repost(s), {unmatched} unmatched)", file=sys.stderr)

    # 4) Output.
    if as_json:
        print(json.dumps({"person": full_name, "profile": poster_url, "originals": rows}, indent=2))
        return 0
    total = sum(r["impressions"] for r in rows)
    print(f"\n{full_name} — {len(rows)} original posts (ranked by impressions)\n")
    print(f"{'impressions':>11} {'react':>6} {'comm':>5} {'shr':>4}  text")
    for r in rows:
        print(f"{r['impressions']:>11,} {r['reactions']:>6} {r['comments']:>5} {r['shares']:>4}  "
              f"{r['text'][:60].replace(chr(10), ' ')!r}")
    print(f"\nTOTAL impressions: {total:,}  | avg {total // max(len(rows), 1):,}")
    print("Pass --json to get machine-readable rows for titling each post.", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client", required=True, help="Client whose MarketBase to write to (matches ~/.env.<Client>)")
    ap.add_argument("--profile", required=True, help="LinkedIn /in/<slug> URL or bare vanity slug")
    ap.add_argument("--limit", type=int, default=20, help="How many latest posts to pull (default 20)")
    ap.add_argument("--unipile-account-id", default=None,
                    help="Owning Unipile account id. Omit to auto-match by the person's name.")
    ap.add_argument("--json", action="store_true", help="Emit JSON for the agent to title posts")
    a = ap.parse_args()
    sys.exit(run(a.client, a.profile, a.limit, a.unipile_account_id, a.json))


if __name__ == "__main__":
    main()
