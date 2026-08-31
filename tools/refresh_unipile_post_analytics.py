#!/usr/bin/env python3
"""Refresh owner-analytics for a connected person's posts from Unipile and
persist the FULL stat set (not just impressions) into the client's MarketBase.

Unipile's /api/v1/users/<provider_id>/posts (queried as the owning account)
returns, per post: impressions_counter (reach), reaction_counter,
comment_counter, repost_counter, mentions (tagged profiles w/ text offsets),
attachments (media w/ mimetype), share_url, parsed_datetime. LinkedIn exposes
reach only to the post owner, so this is owner-analytics — NOT the reactor-list
scraping we avoid.

Persists (no migration — uses existing columns + raw_data jsonb):
  posts.impressions  = impressions_counter
  posts.shares       = repost_counter
  posts.analytics_source='unipile', analytics_at=now()
  posts.raw_data->'unipile_analytics' = {
     impressions, reactions, comments, reposts, share_url, posted_at,
     media:[{type,mimetype}], mentions:[{url,name,is_person}] }

Reposts are skipped (their reach belongs to the original author).

Usage: python3 refresh_unipile_post_analytics.py --client Acme-AI --profile alonrosenberg [--name "Riley Rosenberg"] [--limit 60]
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import post_impressions_report as pir  # reuse _uni_get / resolve_unipile_account
import api_cache


def load_env(p):
    p = os.path.expanduser(p); out = {}
    if not os.path.exists(p): return out
    for line in open(p):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def numeric(s):
    m = re.search(r"(\d{6,})", s or "")
    return m.group(1) if m else None


def mention_objs(it):
    txt = it.get("text") or ""
    out = []
    for m in it.get("mentions") or []:
        url = m.get("url") or ""
        try:
            name = txt[m["start"]:m["start"] + m["length"]]
        except Exception:
            name = None
        out.append({"url": url, "name": name, "is_person": "/in/" in url})
    return out


def media_objs(it):
    out = []
    for a in it.get("attachments") or []:
        if isinstance(a, dict):
            out.append({"type": a.get("type"), "mimetype": a.get("mimetype")})
    return out


def media_label(media):
    """Collapse attachments to a single media label."""
    if not media:
        return "text-only"
    labels = []
    for m in media:
        t, mt = (m.get("type") or ""), (m.get("mimetype") or "")
        if t == "video" or mt.startswith("video"):
            labels.append("video")
        elif t == "img" or mt.startswith("image"):
            labels.append("image")
        elif mt == "application/pdf" or (t == "file" and mt.endswith("pdf")):
            labels.append("document")
        elif t == "file":
            labels.append("document")
        else:
            labels.append(t or "other")
    # de-dupe preserving order
    seen = []
    for l in labels:
        if l not in seen: seen.append(l)
    return "+".join(seen) or "text-only"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--profile", required=True, help="linkedin /in/ slug or url of the connected person")
    ap.add_argument("--name", help="Full name to resolve the Unipile account (default derived from profile)")
    ap.add_argument("--provider-id", help="Member URN (ACoAA…). Default: looked up from leads by profile.")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--refresh", action="store_true",
                    help="Bypass the raw-response cache and re-fetch from Unipile.")
    args = ap.parse_args()

    for src in ("~/.env", f"~/.env.{args.client}"):
        for k, v in load_env(src).items():
            os.environ.setdefault(k, v)
    db_url = load_env(f"~/.env.{args.client}").get("GTM_DB_CONNSTRING")

    import psycopg2
    con = psycopg2.connect(db_url)

    # Resolve provider_id (member URN) + full name from the leads table if not given.
    provider_id, full_name = args.provider_id, args.name
    with con.cursor() as cur:
        cur.execute("""SELECT linkedin_urn, name FROM leads
                       WHERE linkedin_url ILIKE %s AND linkedin_urn IS NOT NULL
                       ORDER BY length(coalesce(linkedin_urn,'')) DESC LIMIT 1""",
                    (f"%{args.profile}%",))
        r = cur.fetchone()
        if r:
            provider_id = provider_id or r[0]
            full_name = full_name or r[1]
    if not provider_id:
        sys.exit("Could not resolve provider_id (member URN). Pass --provider-id.")
    full_name = full_name or args.profile

    acct = pir.resolve_unipile_account(full_name, None)
    print(f"[unipile-analytics] account={acct} provider={provider_id} name={full_name!r}", flush=True)

    # Paginate all posts (cached as one raw blob keyed on provider+limit so a
    # re-run never re-hits Unipile). Stop on a short/empty page or a paging
    # error (Unipile 422s on an exhausted cursor).
    def _fetch_all():
        out, cursor = [], None
        while len(out) < args.limit:
            params = {"account_id": acct, "limit": 50}
            if cursor: params["cursor"] = cursor
            try:
                d = pir._uni_get(f"/api/v1/users/{provider_id}/posts", params)
            except Exception as e:
                print(f"[unipile-analytics] paging stopped: {type(e).__name__}: {str(e)[:80]}", flush=True)
                break
            batch = d.get("items") or []
            out.extend(batch)
            cursor = d.get("cursor")
            if not cursor or len(batch) < 50:
                break
        return out

    items, was_cached = api_cache.cached_call(
        con, "unipile", f"/api/v1/users/{provider_id}/posts",
        {"provider_id": provider_id, "limit": args.limit}, _fetch_all,
        use_cache=not args.refresh)
    items = items or []
    print(f"[unipile-analytics] {len(items)} Unipile post objects "
          f"({'from cache' if was_cached else 'fetched'})", flush=True)

    updated = skipped_repost = unmatched = 0
    for it in items:
        if it.get("is_repost"):
            skipped_repost += 1
            continue
        num = numeric(it.get("social_id") or "")
        if not num:
            continue
        analytics = {
            "impressions": it.get("impressions_counter"),
            "reactions": it.get("reaction_counter"),
            "comments": it.get("comment_counter"),
            "reposts": it.get("repost_counter"),
            "share_url": it.get("share_url"),
            "posted_at": it.get("parsed_datetime"),
            "media": media_objs(it),
            "media_label": media_label(media_objs(it)),
            "mentions": mention_objs(it),
        }
        with con.cursor() as cur:
            cur.execute("""
                UPDATE posts
                   SET impressions = %s,
                       shares = COALESCE(%s, shares),
                       analytics_source = 'unipile',
                       analytics_at = now(),
                       raw_data = jsonb_set(COALESCE(raw_data,'{}'::jsonb),
                                            '{unipile_analytics}', %s::jsonb)
                 WHERE poster_linkedin_url ILIKE %s
                   AND (post_urn LIKE %s OR post_url LIKE %s)
            """, (analytics["impressions"], analytics["reposts"], json.dumps(analytics),
                  f"%{args.profile}%", f"%{num}%", f"%{num}%"))
            if cur.rowcount:
                updated += cur.rowcount
            else:
                unmatched += 1
        con.commit()
    con.close()
    print(f"[unipile-analytics] DONE updated={updated} skipped_repost={skipped_repost} unmatched={unmatched}", flush=True)


if __name__ == "__main__":
    main()
