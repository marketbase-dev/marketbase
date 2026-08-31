#!/usr/bin/env python3
"""fill_basic_profile — cheap Saleleads /user/profile backfill.

For each target lead, fetches the BASIC profile (headline, location,
public_identifier) via Saleleads `/api/v1/user/profile` and UPDATEs the
leads row. Uses COALESCE — never overwrites a non-null existing value.

NOT an enricher. This intentionally only writes core-identity fields on
the leads table; it does NOT write to lead_signals. Used as a cheap
preparation step before the headline-only prefilter runs.

Cost: ~$0.005 per lead (one Saleleads call). Skips leads whose headline
is already populated unless --refresh.

CLI:
  python3 fill_basic_profile.py --client Acme-AI \\
      --lead-file urls.csv [--refresh] [--limit 50]

  python3 fill_basic_profile.py --client Acme-AI \\
      --where-tag potential_thought_leader

  python3 fill_basic_profile.py --client Acme-AI \\
      --where-blank-headline   # only PTLs (or anyone) with empty headline
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, load_client_env, normalize_linkedin_url
from engagers_research import saleleads_get, _slug_from_url
import api_cache

# Honour the global "never re-pay for scraping" rule: /user/profile is the one
# Saleleads endpoint that historically bypassed the cache, so a --refresh re-paid
# for every already-known profile. Read-through within this TTL (free re-use),
# write-through always. A genuinely stale profile (older than the TTL) still
# re-fetches, so --refresh keeps its meaning without paying for unchanged rows.
PROFILE_CACHE_TTL_DAYS = 30


def fetch_basic_profile(linkedin_url: str, conn=None,
                        ttl_days: int = PROFILE_CACHE_TTL_DAYS) -> dict | None:
    """Saleleads /api/v1/user/profile — returns the basic profile dict.
    Saleleads' `username` param accepts both vanity slugs and URN-encoded
    forms; case-preserving (URNs are case-sensitive).

    When `conn` is supplied, reads through enrichment_calls (cache hit within
    `ttl_days` returns the cached raw response, no API call) and writes through
    on a miss — so re-runs / --refresh never re-pay for fresh-enough profiles."""
    slug = _slug_from_url(linkedin_url)
    if not slug:
        return None
    params = {"username": slug}
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT response FROM enrichment_calls
                   WHERE api='saleleads' AND endpoint='/api/v1/user/profile'
                     AND params=%s::jsonb AND success
                     AND fetched_at > now() - make_interval(days => %s)
                   ORDER BY fetched_at DESC LIMIT 1""",
                (api_cache._canon(params), ttl_days))
            row = cur.fetchone()
        if row and isinstance(row[0], dict):
            return row[0].get("data") or None
    res = saleleads_get("/api/v1/user/profile", params)
    if conn is not None and res is not None:
        api_cache.put(conn, "saleleads", "/api/v1/user/profile", params,
                      bool(res.get("success")), res, cost=res.get("cost"))
    if not res or not res.get("success"):
        return None
    return res.get("data") or None


def select_targets(cur, *, lead_url: str | None, lead_file: str | None,
                   where_tag: str | None, where_blank_headline: bool) -> list[dict]:
    base = """
        SELECT id, linkedin_url, name, headline, public_id
        FROM leads
    """
    if lead_url:
        cur.execute(base + " WHERE linkedin_url = %s",
                    (normalize_linkedin_url(lead_url),))
    elif lead_file:
        with open(lead_file, newline="") as f:
            reader = csv.DictReader(f)
            col = next((c for c in ("linkedin_url", "profile_url", "url", "LinkedIn URL")
                        if c in (reader.fieldnames or [])), None)
            if col is None: sys.exit("No URL column in CSV.")
            urls = [normalize_linkedin_url(row[col]) for row in reader if row.get(col)]
        cur.execute(base + " WHERE linkedin_url = ANY(%s)", (urls,))
    elif where_tag:
        cur.execute(base + " WHERE id IN (SELECT lead_id FROM lead_tags WHERE tag=%s)",
                    (where_tag,))
    elif where_blank_headline:
        cur.execute(base + " WHERE headline IS NULL OR headline = ''")
    else:
        return []
    cols = ["id", "linkedin_url", "name", "headline", "public_id"]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Fill basic profile fields on leads via Saleleads /user/profile.")
    ap.add_argument("--client", required=True)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--lead-url")
    target.add_argument("--lead-file")
    target.add_argument("--where-tag")
    target.add_argument("--where-blank-headline", action="store_true",
                        help="Every lead whose headline is NULL or empty.")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-fetch even if headline is already populated.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap per run (0=unlimited).")
    args = ap.parse_args()

    load_client_env(args.client)

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            targets = select_targets(
                cur, lead_url=args.lead_url, lead_file=args.lead_file,
                where_tag=args.where_tag,
                where_blank_headline=args.where_blank_headline)

        if not args.refresh:
            targets = [t for t in targets if not (t["headline"] or "").strip()]

        if args.limit:
            targets = targets[:args.limit]

        if not targets:
            print("No leads need profile backfill.")
            return 0
        print(f"Targets: {len(targets)} lead(s) needing profile backfill")

        # Import the credit-exhausted exception so we can let it propagate
        # out of the per-lead try (calling code will see exit 2 via SystemExit).
        from engagers_research import SaleleadsCreditExhausted as _SaleleadsCreditExhausted

        done = updated = skipped = errors = 0
        for i, t in enumerate(targets, 1):
            try:
                p = fetch_basic_profile(t["linkedin_url"], conn=conn)
                if not p:
                    skipped += 1
                    print(f"[{i}/{len(targets)}] ⊝ skip (no profile): {t['name'] or t['linkedin_url']}")
                    continue
                headline = (p.get("headline") or "").strip() or None
                full_name = (p.get("full_name") or "").strip() or None
                public_id = (p.get("public_identifier") or "").strip() or None
                loc = p.get("location") or {}
                city = (loc.get("city") if isinstance(loc, dict) else "") or None
                country = (loc.get("country") if isinstance(loc, dict) else "") or None
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE leads SET
                          headline   = COALESCE(NULLIF(leads.headline, ''),   %s),
                          name       = COALESCE(leads.name,                   %s),
                          -- public_id: overwrite if missing OR contaminated with URN-format slug
                          public_id  = COALESCE(
                                         CASE WHEN leads.public_id IS NULL
                                              OR leads.public_id = ''
                                              OR leads.public_id LIKE 'ACo%%'
                                              OR leads.public_id LIKE 'ACw%%'
                                           THEN %s
                                           ELSE leads.public_id END,
                                         %s),
                          city       = COALESCE(NULLIF(leads.city, ''),       %s),
                          country    = COALESCE(NULLIF(leads.country, ''),    %s),
                          updated_at = now()
                        WHERE id = %s
                    """, (headline, full_name, public_id, public_id, city, country, t["id"]))
                conn.commit()
                updated += 1
                done += 1
                print(f"[{i}/{len(targets)}] ✓ {t['name'] or full_name or t['linkedin_url']}: "
                      f"headline={(headline or '?')[:60]!r}")
            except _SaleleadsCreditExhausted as e:
                print(f"\n✖ ABORTING: {e}", file=sys.stderr)
                print(f"  Processed {i-1}/{len(targets)}; resume by re-running.",
                      file=sys.stderr)
                return 2
            except Exception as e:
                errors += 1
                print(f"[{i}/{len(targets)}] ✗ {t['name'] or t['linkedin_url']}: {e}")

        print(f"\nSummary: updated={updated}  skipped(no-profile)={skipped}  errors={errors}")
        # Per-process Saleleads cost snapshot (advisory)
        try:
            from engagers_research import saleleads_cost_snapshot, SaleleadsCreditExhausted
            s = saleleads_cost_snapshot()
            print(f"  saleleads: calls={s['calls_total']}  success={s['calls_success']}  "
                  f"charged_denials={s['calls_charged_denial']}  "
                  f"cost_units_charged={s['cost_charged']}  cost_units_free={s['cost_free']}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
