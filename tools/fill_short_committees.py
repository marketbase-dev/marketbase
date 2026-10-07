#!/usr/bin/env python3
"""Second people-search pass for companies whose buying committee came back short.

Expansion runs on Blitz people search with a finance function facet. When a
company yields fewer members than the hand-over cap, the standing assumption has
been that the committee is genuinely small -- at a 40-person firm the only senior
finance person really is the seed.

Measured 2026-09-23 on 20 such companies, that assumption was wrong: Blitz found
42 members, a second LinkedIn index found 39 more that pass the SAME rank and
function gates, and it added members at 20 of 20 companies. The limiting factor
was the function facet, not the companies.

(An open-web search was tried first and is NOT worth repeating: 157 candidates
produced 13 verified people, because 80 of them turned out to work somewhere
other than where the page said. The open web is a bad index for this; a second
LinkedIn index is a good one.)

TRAP, and the reason this is a tool rather than a one-liner: Fresh LinkedIn's
`company_ids` filter is SILENTLY IGNORED -- it returns the unfiltered universe
(22M rows, people at unrelated companies). Only `current_company_ids` filters.
That is the same failure this repo already documents for Blitz's seniority and
department filters. Never "simplify" this back to company_ids.

New members are written as ordinary `buying_committee_expansion` rows carrying
the SAME seed attribution as the company's existing members, so hold
propagation, seed_since and tiering all keep working unchanged. `provider` in
raw_data records where each came from.

    python3 fill_short_committees.py --client <Client> --dry-run
    python3 fill_short_committees.py --client <Client>
    python3 tier_expansion_candidates.py --client <Client>   # then re-tier
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from psycopg.types.json import Jsonb  # noqa: E402
from lib import connect, load_client_env  # noqa: E402
import expand_buying_committee as x  # noqa: E402

FL = "fresh-linkedin-profile-data.p.rapidapi.com"
PROVIDER = "fresh-linkedin-profile-data"
LADDER = ["c_suite", "vp", "director", "head", "controller_family", "manager_family"]

# Companies short of the cap, with the seed attribution their existing members
# carry. `live` excludes held members so a company whose committee was withheld
# is not "topped up" behind the hold.
SHORT_COMPANIES = """
-- Seed webhook dates are materialised ONCE up front. Doing this as a correlated
-- subquery inside HAVING (re-running a jsonb_array_elements join per company)
-- does not finish on a real client DB.
WITH seed_dates AS (
    SELECT lower(sl.linkedin_url) AS seed_url, max(ss.source_date) AS webhooked
    FROM lead_sources ss
    JOIN leads sl ON sl.id = ss.lead_id
    WHERE ss.source_type = %(seed_source_type)s AND sl.linkedin_url IS NOT NULL
    GROUP BY 1
),
mem AS (
    SELECT lower(s.raw_data->>'company_linkedin_url') AS cu,
           s.raw_data->'seed_linkedin_urls'           AS seeds,
           s.raw_data->>'seed_company'                AS seed_company,
           l.id AS lead_id,
           EXISTS (SELECT 1 FROM lead_tags t
                    WHERE t.lead_id = l.id AND t.tag = ANY(%(hold_tags)s)) AS held,
           (SELECT max(d.webhooked)
              FROM jsonb_array_elements_text(s.raw_data->'seed_linkedin_urls') u(su)
              JOIN seed_dates d ON d.seed_url = lower(u.su)) AS seed_webhooked
    FROM leads l
    JOIN lead_sources s ON s.lead_id = l.id AND s.source_type = 'buying_committee_expansion'
    WHERE s.raw_data->>'company_linkedin_url' IS NOT NULL
)
SELECT cu,
       count(*) FILTER (WHERE NOT held) AS live,
       count(*)                          AS total,
       (array_agg(seeds ORDER BY seeds))[1]        AS seeds,
       (array_agg(seed_company ORDER BY seeds))[1] AS seed_company
FROM mem
GROUP BY cu
HAVING count(*) FILTER (WHERE NOT held) > 0
   AND count(*) FILTER (WHERE NOT held) < %(cap)s
   -- optional scope: companies whose original webhook went out on/after a date.
   -- Keyed on when WE sent it, matching the push tool's seed_since, not on when
   -- the client's verdict came back.
   AND (%(seed_since)s::date IS NULL
        OR max(seed_webhooked) >= %(seed_since)s::date)
ORDER BY 2
"""


def _rapid(path, *, body=None, query=None, key):
    h = {"x-rapidapi-key": key, "x-rapidapi-host": FL}
    if body is not None:
        req = urllib.request.Request(f"https://{FL}{path}", data=json.dumps(body).encode(),
                                     headers={**h, "Content-Type": "application/json"},
                                     method="POST")
    else:
        req = urllib.request.Request(
            f"https://{FL}{path}?" + urllib.parse.urlencode(query or {}), headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def search_employees(company_id, keyword, conn, key):
    """Employees of one company matching a keyword. Cached; never re-pays."""
    params = {"current_company_ids": [int(company_id)], "keywords": keyword, "page": 1}
    with conn.cursor() as cur:
        cur.execute("""SELECT response FROM enrichment_calls WHERE api=%s
                       AND endpoint='/search-employees' AND params=%s AND success LIMIT 1""",
                    (PROVIDER, Jsonb(params)))
        row = cur.fetchone()
    if row:
        return (row[0] or {}).get("data") or [], True
    try:
        rid = _rapid("/search-employees", body=params, key=key)["request_id"]
        status = {}
        for _ in range(24):
            status = _rapid("/check-search-status", query={"request_id": rid}, key=key)
            if str(status.get("status", "")).lower() == "done":
                break
            time.sleep(5)
        rows = (_rapid("/get-search-results", query={"request_id": rid}, key=key)
                or {}).get("data") or []
        payload, ok = {"data": rows, "total_count": status.get("total_count")}, True
    except Exception as e:  # noqa: BLE001
        payload, ok, rows = {"_err": f"{type(e).__name__}: {str(e)[:140]}"}, False, []
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO enrichment_calls (api,endpoint,params,success,response)
                       VALUES (%s,'/search-employees',%s,%s,%s)""",
                    (PROVIDER, Jsonb(params), ok, Jsonb(payload)))
        conn.commit()
    return rows, False


def enrich_lead(url, conn, key):
    """Canonical identity + enriched title for a profile. Cached.

    /search-employees identifies people by member URN (/in/ACwAA...). Those URLs
    redirect, but they are NOT canonical: the same person from any other source
    will not match, and leads.linkedin_urn holds ACoAA... URNs -- a different
    namespace -- so neither column can dedupe them. This resolves the URN to
    `linkedin_url` (vanity), `public_id`, an ACoAA `urn`, and the real
    `job_title`, so we store an identity rather than a pointer.

    Rate-limited hard: a 429 must be retried, never recorded as "no data".
    """
    params = {"linkedin_url": url, "include_skills": "false"}
    with conn.cursor() as cur:
        cur.execute("""SELECT response FROM enrichment_calls WHERE api=%s
                       AND endpoint='/enrich-lead' AND params=%s AND success LIMIT 1""",
                    (PROVIDER, Jsonb(params)))
        row = cur.fetchone()
    if row:
        return (row[0] or {}).get("data") or row[0] or {}
    d, ok = {}, False
    for attempt in range(6):
        try:
            d = _rapid("/enrich-lead", query=params, key=key)
            ok = True
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(12 * (attempt + 1))
                continue
            d, ok = {"_err": f"HTTP {e.code}"}, False
            break
        except Exception as e:  # noqa: BLE001
            d, ok = {"_err": f"{type(e).__name__}: {str(e)[:120]}"}, False
            break
    else:
        d, ok = {"_err": "HTTP 429 after retries"}, False
    time.sleep(3)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO enrichment_calls (api,endpoint,params,success,response)
                       VALUES (%s,'/enrich-lead',%s,%s,%s)""",
                    (PROVIDER, Jsonb(params), ok, Jsonb(d)))
        conn.commit()
    return (d or {}).get("data") or d or {}


def company_linkedin_id(url, conn, blitz_key):
    body = {"company_linkedin_url": url}
    with conn.cursor() as cur:
        cur.execute("""SELECT response FROM enrichment_calls WHERE api='blitz'
                       AND endpoint='/v2/enrichment/company' AND params=%s AND success
                       LIMIT 1""", (Jsonb(body),))
        row = cur.fetchone()
    if row:
        return ((row[0] or {}).get("company") or {}).get("linkedin_id")
    req = urllib.request.Request("https://api.blitz-api.ai/v2/enrichment/company",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "x-api-key": blitz_key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d, ok = json.loads(r.read()), True
    except Exception as e:  # noqa: BLE001
        d, ok = {"_err": str(e)[:140]}, False
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO enrichment_calls (api,endpoint,params,success,response)
                       VALUES ('blitz','/v2/enrichment/company',%s,%s,%s)""",
                    (Jsonb(body), ok, Jsonb(d)))
        conn.commit()
    return ((d or {}).get("company") or {}).get("linkedin_id")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--cap", type=int, default=None,
                    help="target committee size; default: the client's handover_cap")
    ap.add_argument("--keywords", default="finance,accounting")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--hold-tags", default="expansion:stale,expansion:filtered_out")
    ap.add_argument("--seed-since", default="", metavar="YYYY-MM-DD",
                    help="only companies whose original webhook went out on/after "
                         "this date (keyed on when we sent it, like the push tool)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    load_client_env(args.client)
    rkey = os.environ.get("RAPIDAPI_KEY")
    bkey = os.environ.get("BLITZAPI_API_KEY_SHARED")
    if not rkey or not bkey:
        sys.exit("RAPIDAPI_KEY and BLITZAPI_API_KEY_SHARED are both required.")
    holds = [t.strip() for t in args.hold_tags.split(",") if t.strip()]
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    cfg_path = (Path(__file__).resolve().parent / "configs" / "buying_committee"
                / f"{args.client}.json")
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    exp = cfg.get("expansion", {})
    keep = exp.get("function_keep", [])
    max_i = LADDER.index(exp.get("min_rank", "controller_family"))
    blocked = [x.norm_company(b) for b in exp.get("company_blocklist", [])]

    with connect(args.client) as conn, conn.cursor() as cur:
        cap = args.cap
        if cap is None:
            try:
                import yaml
                cur.execute("SELECT yaml_spec FROM processors WHERE processor_type="
                            "'targeting_profile' AND superseded_by IS NULL "
                            "ORDER BY created_at DESC LIMIT 1")
                r = cur.fetchone()
                cap = int(((yaml.safe_load(r[0]) or {}).get("buying_committee") or {})
                          .get("handover_cap", 5)) if r else 5
            except Exception:  # noqa: BLE001
                cap = 5
        print(f"{args.client}: target committee size {cap}, keywords {keywords}")

        cur.execute(SHORT_COMPANIES, {
            "hold_tags": holds, "cap": cap,
            "seed_since": args.seed_since or None,
            "seed_source_type": exp.get("seed_source_type",
                                        "buyer_monitor_likely_to_connect")})
        short = cur.fetchall()
        if args.seed_since:
            print(f"scope: original webhook sent on/after {args.seed_since}")
    if args.limit:
        short = short[:args.limit]
    print(f"companies below the cap: {len(short)}\n")

    added = scanned = skipped = unresolved = 0
    with connect(args.client) as conn:
        cur = conn.cursor()
        for i, (cu, live, total, seeds, seed_company) in enumerate(short, 1):
            if blocked and any(x.company_matches(x.norm_company(seed_company or ""), b)
                               for b in blocked):
                skipped += 1
                continue
            cid = company_linkedin_id(cu, conn, bkey)
            if not cid:
                print(f"  [{i}/{len(short)}] {str(seed_company)[:30]:30} company id unresolved")
                continue
            cur.execute("SELECT lower(linkedin_url) FROM leads WHERE linkedin_url IS NOT NULL")
            known = {r[0] for r in cur.fetchall()}  # compared case-insensitively

            seen, keepers = set(), []
            for kw in keywords:
                rows, _ = search_employees(cid, kw, conn, rkey)
                for p in rows:
                    # NEVER lowercase this URL. /search-employees returns the
                    # profile as a member URN (/in/ACwAA...), and LinkedIn URNs
                    # are CASE-SENSITIVE: the lowercased form 404s on every
                    # provider, which silently makes the member unverifiable and
                    # unresolvable. Compare case-insensitively, store verbatim.
                    u = (p.get("linkedin_url") or "").rstrip("/")
                    if not u or u.lower() in seen:
                        continue
                    seen.add(u.lower())
                    ti = p.get("job_title") or ""
                    rank = x.rank_of(ti)
                    if not rank or LADDER.index(rank) > max_i:
                        continue
                    if not x.function_ok(ti, keep, rank):
                        continue
                    # Resolve the URN to a canonical identity BEFORE keeping the
                    # person: store a vanity URL and an ACoAA urn, never the
                    # search's ACwAA pointer, or the same human silently lands in
                    # the table twice under two different URLs.
                    e = enrich_lead(u, conn, rkey)
                    canon = (e.get("linkedin_url") or "").rstrip("/")
                    if not canon:
                        unresolved += 1
                        continue
                    # Re-gate on the ENRICHED title. A search snippet is not a
                    # qualification decision.
                    real_ti = e.get("job_title") or ti
                    rank = x.rank_of(real_ti)
                    if not rank or LADDER.index(rank) > max_i:
                        continue
                    if not x.function_ok(real_ti, keep, rank):
                        continue
                    if canon.lower() in known:
                        continue
                    keepers.append({"url": canon, "urn": e.get("urn"),
                                    "public_id": e.get("public_id"),
                                    "name": e.get("full_name") or p.get("full_name"),
                                    "title": real_ti, "search_title": ti,
                                    "rank": rank, "location": e.get("location"),
                                    "employees": e.get("company_employee_count"),
                                    "company_url": e.get("company_linkedin_url"),
                                    "raw": p, "enriched": e})
            scanned += len(seen)
            print(f"  [{i}/{len(short)}] {str(seed_company)[:30]:30} had={live} "
                  f"scanned={len(seen):>3} new={len(keepers)}")
            if args.dry_run or not keepers:
                added += len(keepers)
                continue

            cur.executemany("""INSERT INTO leads (linkedin_url, linkedin_urn, public_id,
                                   name, current_title, current_company,
                                   current_company_url, created_at, updated_at)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
                               ON CONFLICT (linkedin_url) DO UPDATE SET
                                 linkedin_urn = COALESCE(leads.linkedin_urn, EXCLUDED.linkedin_urn),
                                 public_id = COALESCE(leads.public_id, EXCLUDED.public_id),
                                 name = COALESCE(NULLIF(leads.name,''), EXCLUDED.name),
                                 current_title = COALESCE(NULLIF(leads.current_title,''),
                                                          EXCLUDED.current_title),
                                 updated_at = NOW()""",
                            [(k["url"], k.get("urn"), k.get("public_id"), k["name"],
                              k["title"], (k["raw"].get("company") or seed_company),
                              k.get("company_url") or cu) for k in keepers])
            cur.execute("SELECT linkedin_url, id FROM leads WHERE linkedin_url = ANY(%s)",
                        ([k["url"] for k in keepers],))
            ids = dict(cur.fetchall())
            cur.executemany("""INSERT INTO lead_sources
                                 (lead_id, source_type, source_label, source_date, raw_data)
                               VALUES (%s,'buying_committee_expansion',%s,%s,%s)""",
                            [(ids[k["url"]],
                              f"{args.client.lower()} short-committee fill ({PROVIDER})",
                              date.today(),
                              Jsonb({"seed_linkedin_urls": seeds,
                                     "seed_company": seed_company,
                                     "company_linkedin_url": cu,
                                     "rank": k["rank"],
                                     "matched_title": k["title"],
                                     "search_title": k.get("search_title"),
                                     "company_employee_count": k.get("employees"),
                                     "provider": PROVIDER,
                                     "identity": "canonical (enriched from member urn)",
                                     "fresh_linkedin": k["raw"]}))
                             for k in keepers if ids.get(k["url"])])
            cur.executemany("""INSERT INTO lead_tags (lead_id, tag, notes, tagged_by)
                               VALUES (%s,'expansion:buying_committee',%s,'fill_short_committees')
                               ON CONFLICT (lead_id, tag) DO NOTHING""",
                            [(ids[k["url"]], f"second-pass fill; committee was {live} of {cap}")
                             for k in keepers if ids.get(k["url"])])
            conn.commit()
            added += len(keepers)

    print(f"\ncompanies processed : {len(short)}  (blocklisted, skipped: {skipped})")
    print(f"profiles scanned    : {scanned}")
    print(f"dropped, no canonical profile: {unresolved}")
    print(f"members {'that would be added' if args.dry_run else 'added'}: {added}")
    if not args.dry_run and added:
        print(f"\nnext: python3 tier_expansion_candidates.py --client {args.client}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
