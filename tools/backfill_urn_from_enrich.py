#!/usr/bin/env python3
"""Backfill leads.linkedin_urn for leads stored with a vanity URL and no URN.

Why: marketbase-sync-conversations matches Unipile chats on the LinkedIn member URN
(the `ACoAA…` `attendee_provider_id`). Leads ingested with a vanity URL
(/in/janedoe) and no `leads.linkedin_urn` can never be matched, so their
conversations are invisible. This resolves each vanity URL to its `ACoAA…` URN
via Fresh LinkedIn Profile Data `/enrich-lead` (the same by-URL endpoint
fill_follower_count_fresh.py uses) and backfills `linkedin_urn` (plus refreshes
title/company/location while we have the payload). After this runs, re-run
sync_conversations.py for the same leads and their threads pull in.

Read-through/write-through cached in `enrichment_calls` (never re-pays). DB-only
output — no disk artifacts (per the MarketBase conventions).

Default target set = every `they:replied` lead whose URL is vanity AND whose
`linkedin_urn` is null/non-URN (the exact "unrecognized replier" gap). Override
with --where or --lead-file.

Usage:
    set -a; source ~/.env; source ~/.env.Acme; set +a
    python3 backfill_urn_from_enrich.py --client Acme [--limit N] [--refresh] [--dry-run]
"""
from __future__ import annotations
import argparse, json, os, re, sys, urllib.parse, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, load_client_env
import api_cache

# Fresh Profile Data's /enrich-lead `country` field reflects the COMPANY HQ, not
# the person (e.g. an SAP employee in Bengaluru comes back country=Germany). That
# silently bypassed the out-of-geo DQ. So we prefer the country embedded in the
# person's own location string ("Bengaluru, Karnataka, India" -> India). (alice,
# 2026-07-21.)
_KNOWN_COUNTRIES = {
    "united states", "usa", "us", "united kingdom", "uk", "canada", "germany",
    "france", "india", "pakistan", "bangladesh", "sri lanka", "nepal",
    "indonesia", "vietnam", "philippines", "thailand", "malaysia", "israel",
    "netherlands", "spain", "italy", "switzerland", "sweden", "norway",
    "denmark", "finland", "ireland", "belgium", "austria", "poland", "portugal",
    "greece", "turkey", "australia", "new zealand", "singapore", "japan",
    "china", "hong kong", "south korea", "taiwan", "brazil", "mexico",
    "argentina", "colombia", "chile", "south africa", "nigeria", "kenya",
    "egypt", "united arab emirates", "uae", "saudi arabia", "qatar", "romania",
    "czechia", "czech republic", "hungary", "ukraine", "russia", "luxembourg",
}
_COUNTRY_CANON = {"usa": "United States", "us": "United States",
                  "uk": "United Kingdom", "uae": "United Arab Emirates"}


def _country_from_location(loc):
    """Country from a LinkedIn location string's trailing token, e.g.
    'Bengaluru, Karnataka, India · Hybrid' -> 'India'. None if the last
    comma-separated token isn't a recognized country."""
    if not loc:
        return None
    cleaned = re.split(r"[·(]", loc)[0].strip()   # drop "· Hybrid" / "(On-site)"
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    if not parts:
        return None
    key = parts[-1].lower()
    if key in _KNOWN_COUNTRIES:
        return _COUNTRY_CANON.get(key, parts[-1])
    return None
import psycopg

HOST = "fresh-linkedin-profile-data.p.rapidapi.com"
AC = re.compile(r"AC[A-Za-z0-9_\-]{15,}")

# the "unsyncable replied lead" set: they:replied, vanity URL, no real URN
DEFAULT_WHERE = (
    "l.id IN (SELECT lt.lead_id FROM lead_tags lt WHERE lt.tag='they:replied') "
    "AND l.linkedin_url !~ 'AC[A-Za-z0-9_-]{15,}' "
    "AND (l.linkedin_urn IS NULL OR l.linkedin_urn !~ 'AC[A-Za-z0-9_-]{15,}') "
    # skip rows already found to be duplicates of a canonical URN-form lead
    "AND l.id NOT IN (SELECT lead_id FROM lead_tags WHERE tag='flag:duplicate_lead')"
)


def enrich_lead(key: str, url: str) -> dict | None:
    u = f"https://{HOST}/enrich-lead?" + urllib.parse.urlencode(
        {"linkedin_url": url, "include_skills": "false"})
    req = urllib.request.Request(u, headers={"x-rapidapi-key": key, "x-rapidapi-host": HOST})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=45).read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--where", default=DEFAULT_WHERE, help="SQL WHERE over leads l (default = unsyncable replied set)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--refresh", action="store_true", help="ignore cache, re-fetch")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    env = load_client_env(args.client)
    key = os.environ.get("FRESH_LINKEDIN_DATA_API_KEY") or env.get("FRESH_LINKEDIN_DATA_API_KEY")
    if not key:
        sys.exit("FRESH_LINKEDIN_DATA_API_KEY missing (source ~/.env).")

    conn = connect(args.client)
    sql = f"SELECT l.id, l.name, l.linkedin_url FROM leads l WHERE {args.where} ORDER BY l.name"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    with conn.cursor() as c:
        c.execute(sql)
        targets = c.fetchall()
    print(f"{len(targets)} leads to resolve", flush=True)

    upd = miss = err = dup = 0
    for i, (lid, name, url) in enumerate(targets, 1):
        data, _hit = api_cache.cached_call(
            conn, "fresh-linkedin-profile-data", "/enrich-lead",
            {"linkedin_url": url}, lambda: enrich_lead(key, url),
            use_cache=not args.refresh)
        d = (data or {}).get("data") or {}
        urn_raw = d.get("urn") if isinstance(d, dict) else None
        m = AC.search(urn_raw or "")
        if not m:
            miss += 1
            print(f"  [{i}/{len(targets)}] NO URN  {name}  {url}", flush=True)
            continue
        urn = m.group(0)
        if args.dry_run:
            print(f"  [{i}/{len(targets)}] {name} -> {urn}", flush=True)
            upd += 1
            continue
        # backfill urn + refresh profile fields present in the payload
        # Prefer the country embedded in the person's own location over the
        # payload's company-HQ country (the geo-DQ bypass fix).
        loc_country = (_country_from_location(d.get("city"))
                       or _country_from_location(d.get("location")))
        fields = {
            "linkedin_urn": urn,
            "current_title": d.get("job_title"),
            "current_company": d.get("company"),
            "city": d.get("city"),
            "country": loc_country or d.get("country"),
            "headline": d.get("headline"),
        }
        sets = ["linkedin_urn=%(linkedin_urn)s", "last_enriched_at=now()"]
        params = {"id": lid, "linkedin_urn": urn}
        for col in ("current_title", "current_company", "city", "country", "headline"):
            if fields[col] not in (None, ""):
                sets.append(f"{col}=%({col})s")
                params[col] = fields[col]
        try:
            with conn.cursor() as c:
                c.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=%(id)s", params)
            conn.commit()
            upd += 1
        except psycopg.errors.UniqueViolation:
            # this vanity row resolves to a URN already owned by a canonical
            # URN-form lead -> it's a duplicate. Don't backfill; flag it for a
            # separate merge so it stops re-surfacing as an "unsynced replier".
            conn.rollback()
            with conn.cursor() as c:
                c.execute("SELECT id, linkedin_url FROM leads WHERE linkedin_urn ~ %s AND id<>%s LIMIT 1",
                          (urn, lid))
                canon = c.fetchone()
                note = (f"duplicate of lead {canon[0]} ({canon[1]}); resolved urn {urn} "
                        f"already owned by the canonical URN-form row") if canon else f"resolved urn {urn} already owned"
                c.execute("""INSERT INTO lead_tags (lead_id, tag, notes, tagged_by)
                             VALUES (%s,'flag:duplicate_lead',%s,'backfill_urn_from_enrich')
                             ON CONFLICT (lead_id, tag) DO UPDATE SET notes=EXCLUDED.notes""",
                          (lid, note))
            conn.commit()
            dup += 1
            print(f"  [{i}/{len(targets)}] DUP   {name}  -> {note}", flush=True)
            continue
        if i % 10 == 0 or i == len(targets):
            print(f"  [{i}/{len(targets)}] backfilled={upd} dup={dup} no-urn={miss}", flush=True)

    print(f"\n=== done: backfilled={upd}  dup={dup}  no-urn={miss}  total={len(targets)} ===")
    if upd and not args.dry_run:
        print("Next: re-run sync_conversations.py for these leads to pull their threads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
