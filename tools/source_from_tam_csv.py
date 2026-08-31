#!/usr/bin/env python3
"""Company-first lead sourcing from a TAM CSV (Acme Plan B).

For a CSV of target companies (Company, Employees, Website, ...):
  1. RESOLVE each company to a LinkedIn company (slug + saleleads_id + headcount)
     via a waterfall: existing MarketBase slug -> Saleleads /company/profile (reliable);
     else name -> /company/profile with a headcount>=min_employees validation gate
     (rejects wrong-namesake small matches). LeadMagic domain lookup is unavailable
     (out of credits) so unresolved companies are reported, not guessed.
  2. UPSERT resolved companies into `companies` (key = linkedin_slug), storing the
     source-filename tag in raw_data.source_tags (deduped). Idempotent.
  3. PEOPLE SEARCH per resolved company: Saleleads /search/people with
     current_company=<saleleads_id> across persona keywords, paginated. Collect
     real LinkedIn URLs + title + location. Light title-relevance + geo heuristics.
  4. DEDUPE against existing MarketBase leads (by linkedin_url) and REPORT new-lead yield.
     Does NOT upload leads — yield-estimation first (per user). Writes people to a
     JSON sidecar only when --emit-people is passed (for a follow-up upload step).

Usage:
  python3 source_from_tam_csv.py --client Acme \
      --csv "<path>" --source-tag "Copperhelm_TAM_Top200_v1 - Backup Pool" \
      [--min-employees 1000] [--keywords "CISO,Cloud Security,Head of Security,VP Security"] \
      [--max-pages 4] [--limit N] [--no-upsert] [--no-apollo]

Writes EVERYTHING directly to the client's MarketBase — companies upsert + leads upsert
+ lead_sources (source_type=linkedin_people_search, source_label=<source-tag>). No
disk artifacts. Idempotent / resumable: a company already carrying a lead_source for
this source_label is skipped on re-run.
"""
import argparse, csv, json, os, re, subprocess, sys, time
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import normalize_linkedin_url, linkedin_urn, resolve_canonical_url

SALELEADS = os.path.expanduser("~/.claude/tools/saleleads/saleleads_cli.py")

TARGET_GEOS = {  # full country names (qualifier's TARGET_GEOS, lowercased)
    "united states","usa","us","canada","united kingdom","uk","england",
    "scotland","wales","northern ireland","ireland","france","germany",
    "netherlands","belgium","luxembourg","switzerland","austria","spain",
    "portugal","italy","sweden","norway","denmark","finland","iceland",
}
US_STATES = {  # 2-letter; location strings like "Orlando, FL"
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
    "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
    "va","wa","wv","wi","wy","dc",
}
US_NAMES = {"united states", "usa", " us", "u.s."}
CA_NAMES = {"canada"}
CA_PROV = {"ontario","quebec","british columbia","alberta","manitoba","saskatchewan",
           "nova scotia","new brunswick","newfoundland","prince edward island",
           "on","qc","bc","ab","mb","sk","ns","nb","nl","pe",  # province codes
           "toronto","vancouver","montreal","montréal","calgary","ottawa","edmonton","waterloo"}
EU_NAMES = {"united kingdom","uk","england","scotland","wales","northern ireland","ireland",
            "france","germany","netherlands","belgium","luxembourg","switzerland","austria",
            "spain","portugal","italy","sweden","norway","denmark","finland","iceland"}
# Title relevance — a "relevant target" must look security/cloud-security senior-ish.
RELEVANT_RE = re.compile(
    r"\b(ciso|cso|chief (information )?security|information security|infosec|"
    r"cyber\s*security|cybersecurity|cloud security|security architect|"
    r"security engineer|appsec|application security|product security|"
    r"head of security|security operations|secops|devsecops|cspm|cnapp|"
    r"vp.{0,12}security|security leader|security officer)\b", re.IGNORECASE)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def sl(args_list):
    r = subprocess.run(["python3", SALELEADS] + args_list,
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}


def company_profile(query: str) -> dict:
    d = sl(["company-profile", "--name", query]).get("data") or {}
    if not d:
        return {}
    rng = d.get("employee_count_range")
    if isinstance(rng, dict):
        lo, hi = rng.get("start"), rng.get("end")
        rng = f"{lo or ''}-{hi or ''}".strip("-") if (lo or hi) else ""
    inds = d.get("industries")
    if isinstance(inds, list):
        inds = inds[0] if inds else ""
    if isinstance(inds, dict):
        inds = inds.get("name") or ""
    return {
        "id": str(d.get("id")) if d.get("id") else "",
        "slug": d.get("universal_name") or "",
        "name": d.get("name") or "",
        "emp": d.get("employee_count") or 0,
        "emp_range": str(rng or ""),
        "linkedin_url": d.get("linkedin_url") or "",
        "website": d.get("website_url") or "",
        "industry": str(inds or ""),
    }


SUFFIXES = {"laboratories","laboratory","technology","technologies","resources",
            "holdings","holding","hotels","worldwide","corporation","corp","company",
            "companies","inc","incorporated","group","international","plc","co",
            "industries","systems","solutions","communications","financial","brands",
            "stores","enterprises","motor","motors"}


# Apollo organization-enrichment (domain -> real LinkedIn slug). Works by domain
# even on accounts where mixed_people/api_search redacts PII.
# Multiple accounts, tried SERIALLY: on any auth/credit failure for a key we mark
# it dead for the rest of the session and fall through to the next key. Apollo
# signals exhausted credits with HTTP 422 (NOT 402) — that bug caused the first
# sweep to never rotate off the exhausted 'acme' key.
import urllib.request as _ur, urllib.error as _ue
from lib import secrets
# Resolved from Infisical / env. Set APOLLO_API_KEYS as a comma-separated pool.
APOLLO_KEYS = [(f"key{i+1}", k) for i, k in enumerate(secrets.get_list("APOLLO_API_KEYS"))]
_apollo_dead = set()           # key strings that returned auth/credit errors this session
DEAD_CODES = (401, 402, 403, 422)  # invalid / forbidden / insufficient-credits → skip key for session


def full_domain(website: str) -> str:
    if not website:
        return ""
    d = re.sub(r"^https?://", "", website.strip().lower()).split("/")[0]
    return re.sub(r"^www\.", "", d)


def apollo_slug_from_domain(domain: str) -> str:
    """domain -> LinkedIn slug via Apollo org-enrich. Tries each live account
    serially; remembers dead keys for the session. Returns '' if none succeed."""
    if not domain:
        return ""
    for name, key in APOLLO_KEYS:
        if key in _apollo_dead:
            continue
        try:
            req = _ur.Request(f"https://api.apollo.io/v1/organizations/enrich?domain={domain}",
                              headers={"X-Api-Key": key, "User-Agent": "curl/7.88.1",
                                       "Cache-Control": "no-cache", "Content-Type": "application/json"})
            with _ur.urlopen(req, timeout=40) as r:
                org = (json.loads(r.read()).get("organization") or {})
            li = org.get("linkedin_url") or ""
            m = re.search(r"/company/([^/?#]+)", li)
            return m.group(1) if m else ""
        except _ue.HTTPError as e:
            if e.code in DEAD_CODES:
                _apollo_dead.add(key)
                print(f"  [apollo] key {name} dead (HTTP {e.code}) — skipping for session", flush=True)
                continue          # serial fallback → next account
            if e.code == 429:
                continue          # rate-limited now; try next account, keep this one live
            return ""             # other HTTP error for this domain — give up on it
        except Exception:
            continue              # network hiccup — try next account
    return ""


def domain_root(website: str) -> str:
    if not website:
        return ""
    return full_domain(website).split(".")[0]


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    s = re.sub(r"^the ", "", s)
    return re.sub(r"\s+", "-", s)


def candidate_slugs(company, website, gtmdb_by_name):
    """Prioritized candidate LinkedIn slugs to try, with provenance label."""
    cands = []
    nm = norm(company)
    for (gname, slug, sid, emp) in gtmdb_by_name.get(nm, []):
        if slug and not slug.startswith("unknown") and "/" not in slug:
            cands.append((slug, "gtmdb_slug"))
    dr = domain_root(website)
    if dr:
        cands.append((dr, "domain_root"))
    full = slugify(company)
    if full:
        cands.append((full, "slug_full"))
    words = full.split("-")
    if len(words) > 1 and words[-1] in SUFFIXES:
        cands.append(("-".join(words[:-1]), "slug_nosuffix"))
    cands.append((company, "name"))  # raw name as last resort
    # dedupe preserving order
    seen, out = set(), []
    for c, m in cands:
        if c and c.lower() not in seen:
            seen.add(c.lower()); out.append((c, m))
    return out


def _accept(p, method, csv_emp, min_emp):
    emp = p.get("emp") or 0
    # Headcount floor + order-of-magnitude match to the CSV (rejects small namesakes
    # like "up"=1199 for Union Pacific=30336 even though both clear 1000).
    if emp >= min_emp and (not csv_emp or 0.15 <= emp / max(csv_emp, 1) <= 7):
        return p, method
    if emp == 0 and method in ("domain_root", "gtmdb_slug", "apollo_domain"):
        return p, method + "_nosizemaybe"
    return None, None


def resolve(company, website, csv_emp, min_emp, gtmdb_by_name, use_apollo=True):
    """Try candidate slugs in priority order, then Apollo domain-enrich as fallback.
    Returns (profile_dict, method) or (None, 'unresolved')."""
    for cand, method in candidate_slugs(company, website, gtmdb_by_name):
        p = company_profile(cand)
        if not p.get("id"):
            continue
        acc, m = _accept(p, method, csv_emp, min_emp)
        if acc:
            return acc, m
    # Fallback: Apollo org-enrich(domain) -> real LinkedIn slug -> Saleleads profile.
    if use_apollo:
        slug = apollo_slug_from_domain(full_domain(website))
        if slug:
            p = company_profile(slug)
            if p.get("id"):
                acc, m = _accept(p, "apollo_domain", csv_emp, min_emp)
                if acc:
                    return acc, m
    return None, "unresolved"


def _is_us(l):
    if any(n in l for n in US_NAMES):
        return True
    return any(t.strip() in US_STATES for t in l.split(","))


def _is_ca(l):
    if any(n in l for n in CA_NAMES):
        return True
    return any(t.strip() in CA_PROV for t in re.split(r"[,/]", l))


def geo_ok(loc: str, mode: str = "us_eu") -> bool:
    """mode: 'us_ca' (US + Canada only), 'us_eu' (US/CA/UK/EU), 'all'."""
    if not loc:
        return False
    l = loc.lower()
    if mode == "all":
        return True
    if _is_us(l) or _is_ca(l):
        return True
    if mode == "us_ca":
        return False
    return any(n in l for n in EU_NAMES)  # us_eu


def search_people(company_id, keyword, max_pages):
    """Returns (people, meta) where meta = {total, returned, capped}."""
    out = sl(["list-people", "--keyword", keyword, "--company-id", company_id,
              "--max-pages", str(max_pages), "--throttle", "0.4"])
    if not isinstance(out, dict):
        return [], {"total": 0, "returned": 0, "capped": False}
    people = out.get("people", [])
    total = out.get("total")
    meta = {"total": total if isinstance(total, int) else len(people),
            "returned": len(people),
            "capped": bool(out.get("capped"))}
    return people, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--csv", help="companies CSV (required unless --from-marketbase-tag)")
    ap.add_argument("--from-marketbase-tag", help="skip CSV + resolution; load already-resolved companies "
                    "(those with a saleleads_id) whose companies.raw_data.source_tags contains this tag.")
    ap.add_argument("--source-tag", required=True)
    ap.add_argument("--min-employees", type=int, default=1000)
    ap.add_argument("--keywords", default="CISO,Cloud Security,Head of Security,VP Security")
    ap.add_argument("--max-pages", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0, help="cap # companies (0=all)")
    ap.add_argument("--no-upsert", action="store_true", help="skip company upsert (companies must already exist)")
    ap.add_argument("--no-apollo", action="store_true", help="disable Apollo domain-enrich fallback")
    ap.add_argument("--source-type", default="linkedin_people_search")
    ap.add_argument("--round", required=True,
                    help="list-building-round identifier, stored on every lead_sources row "
                         "(e.g. 'tam_seniors_us_ca_2026-06-18'). Resume + dedup are round-aware.")
    ap.add_argument("--geo", choices=["us_ca", "us_eu", "all"], default="us_eu",
                    help="geography filter applied client-side on each person's location.")
    ap.add_argument("--no-resume", action="store_true",
                    help="re-search companies even if already done for this round")
    ap.add_argument("--company-batch", type=int, default=1,
                    help="batch N companies per search call (comma-separated current_company IDs, "
                         "ORed). >1 trades per-company attribution for ~Nx fewer calls — fine when "
                         "qualification enrichment recovers each person's company anyway.")
    args = ap.parse_args()

    env = {}
    envp = Path.home() / f".env.{args.client}"
    for line in envp.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
    conn = psycopg2.connect(env["GTM_DB_CONNSTRING"]); conn.autocommit = True
    cur = conn.cursor()

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    resolved, unresolved = [], []

    # ===== SOURCE COMPANIES FROM MarketBase (already resolved) — no CSV, no Apollo =====
    if args.from_gtmdb_tag:
        cur.execute("""SELECT name, linkedin_slug, saleleads_id FROM companies
                       WHERE saleleads_id IS NOT NULL AND raw_data->'source_tags' ? %s
                       ORDER BY employee_count DESC NULLS LAST""", (args.from_gtmdb_tag,))
        for n, slug, sid in cur.fetchall():
            resolved.append({"name": n, "slug": slug, "id": str(sid),
                             "linkedin_url": "", "website": "", "industry": "",
                             "emp": None, "emp_range": "", "method": "gtmdb_tag"})
        if args.limit:
            resolved = resolved[:args.limit]
        print(f"== Loaded {len(resolved)} resolved companies from MarketBase tag '{args.from_gtmdb_tag}' ==", flush=True)

    if not args.from_gtmdb_tag:
      cur.execute("SELECT name, linkedin_slug, saleleads_id, employee_count FROM companies WHERE name IS NOT NULL")
      gtmdb_by_name = {}
      for n, slug, sid, emp in cur.fetchall():
        gtmdb_by_name.setdefault(norm(n), []).append((n, slug, sid, emp))

      rows = list(csv.DictReader(open(args.csv)))
      if args.limit:
        rows = rows[:args.limit]

      print(f"== Resolving {len(rows)} companies (min_employees={args.min_employees}) ==", flush=True)
      for i, r in enumerate(rows, 1):
        company = r.get("Company") or r.get("company") or ""
        try:
            csv_emp = int(re.sub(r"[^0-9]", "", r.get("Employees", "") or "0") or 0)
        except Exception:
            csv_emp = 0
        website = r.get("Website") or r.get("website") or ""
        p, method = resolve(company, website, csv_emp, args.min_employees, gtmdb_by_name,
                            use_apollo=not args.no_apollo)
        if p:
            p["csv_company"] = company; p["method"] = method
            resolved.append(p)
            if not args.no_upsert:
                cur.execute("""
                  INSERT INTO companies (linkedin_slug, linkedin_url, name, website,
                      industry, employee_count, employee_range, saleleads_id, raw_data, size_fetched_at)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                  ON CONFLICT (linkedin_slug) DO UPDATE SET
                      saleleads_id   = COALESCE(EXCLUDED.saleleads_id, companies.saleleads_id),
                      linkedin_url   = COALESCE(NULLIF(EXCLUDED.linkedin_url,''), companies.linkedin_url),
                      name           = COALESCE(companies.name, EXCLUDED.name),
                      website        = COALESCE(NULLIF(EXCLUDED.website,''), companies.website),
                      industry       = COALESCE(NULLIF(EXCLUDED.industry,''), companies.industry),
                      employee_count = COALESCE(EXCLUDED.employee_count, companies.employee_count),
                      employee_range = COALESCE(NULLIF(EXCLUDED.employee_range,''), companies.employee_range),
                      raw_data = jsonb_set(
                          COALESCE(companies.raw_data,'{}'::jsonb), '{source_tags}',
                          (SELECT to_jsonb(array(SELECT DISTINCT unnest(
                              COALESCE(ARRAY(SELECT jsonb_array_elements_text(companies.raw_data->'source_tags')), ARRAY[]::text[])
                              || ARRAY[%s]))) ),
                          true),
                      updated_at = now()
                """, (p["slug"], p["linkedin_url"], p["name"], p["website"], p["industry"] or "",
                      p["emp"] or None, p["emp_range"], p["id"] or None,
                      Json({"source_tags": [args.source_tag]}), args.source_tag))
        else:
            unresolved.append(company)
        if i % 25 == 0:
            print(f"  ...{i}/{len(rows)} resolved={len(resolved)} unresolved={len(unresolved)}", flush=True)

      print(f"\n== Resolved {len(resolved)}/{len(rows)} | unresolved {len(unresolved)} ==", flush=True)
      by_method = {}
      for p in resolved:
        by_method[p["method"]] = by_method.get(p["method"], 0) + 1
      print(f"   by method: {by_method}")
      if not args.no_upsert:
        print(f"   upserted {len(resolved)} companies, tagged source='{args.source_tag}'")

    # ---- People search → write leads + lead_sources DIRECTLY to MarketBase ----
    # No disk. Continuous writes also keep the Neon connection warm (the prior
    # in-memory design idled it out and lost everything at the dedup step).
    print(f"\n== People search → MarketBase: keywords={keywords} max_pages={args.max_pages} ==", flush=True)
    cap = args.max_pages * 10  # ~10 results/page
    src_label = args.source_tag
    new_leads = existing_leads = sources_added = searched = skipped = 0
    capped_report = []

    def db():
        """Return a live cursor, reconnecting if Neon dropped the connection."""
        nonlocal conn, cur
        try:
            cur.execute("SELECT 1")
            return cur
        except Exception:
            try: conn.close()
            except Exception: pass
            conn = psycopg2.connect(env["GTM_DB_CONNSTRING"]); conn.autocommit = True
            cur = conn.cursor()
            return cur

    def upsert_lead_and_source(c, person, company_name, company_url, prov):
        """UPSERT a person into leads + append a round-tagged lead_sources row.
        Returns (lead_id, inserted) or None if the URL was unusable."""
        raw_url = person.get("url") or ""
        url = normalize_linkedin_url(raw_url)
        if not url:
            return None
        title = person.get("title", "") or ""
        loc = person.get("location", "") or ""
        url = resolve_canonical_url(c, url, urn_hint=linkedin_urn(url))
        c.execute("""
            INSERT INTO leads (linkedin_url, linkedin_urn, name, headline, current_title,
                               current_company, current_company_url, city)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (linkedin_url) DO UPDATE SET
                name=COALESCE(leads.name,EXCLUDED.name),
                headline=COALESCE(leads.headline,EXCLUDED.headline),
                current_title=COALESCE(leads.current_title,EXCLUDED.current_title),
                current_company=COALESCE(leads.current_company,EXCLUDED.current_company),
                current_company_url=COALESCE(leads.current_company_url,EXCLUDED.current_company_url),
                city=COALESCE(leads.city,EXCLUDED.city),
                linkedin_urn=COALESCE(leads.linkedin_urn,EXCLUDED.linkedin_urn),
                updated_at=now()
            RETURNING id, (xmax = 0) AS inserted
        """, (url, linkedin_urn(url), person.get("name") or None, title or None, title or None,
              company_name, company_url, loc or None))
        lead_id, inserted = c.fetchone()
        c.execute("""INSERT INTO lead_sources (lead_id, source_type, source_label, source_date, raw_data)
                     SELECT %s,%s,%s,CURRENT_DATE,%s::jsonb
                     WHERE NOT EXISTS (SELECT 1 FROM lead_sources WHERE lead_id=%s
                       AND source_type=%s AND source_label=%s AND raw_data->>'round'=%s
                       AND raw_data->>'matched_keyword'=%s)""",
                  (lead_id, args.source_type, src_label, json.dumps({**prov, "title": title, "location": loc}),
                   lead_id, args.source_type, src_label, args.round, prov.get("matched_keyword")))
        return lead_id, inserted

    # ===== BATCH MODE: N companies per search call (comma-separated IDs, ORed) =====
    if args.company_batch and args.company_batch > 1:
        N = args.company_batch
        batches = [resolved[i:i+N] for i in range(0, len(resolved), N)]
        print(f"   batch mode: {len(batches)} batches of up to {N} companies", flush=True)
        for bi, batch in enumerate(batches, 1):
            ids = ",".join(p["id"] for p in batch if p["id"])
            slugs = [p["slug"] for p in batch]
            sids = [p["id"] for p in batch]
            seen_b = set()
            for kw in keywords:
                ppl, _meta = search_people(ids, kw, args.max_pages)
                for person in ppl:
                    key = (person.get("url") or "").split("?")[0].rstrip("/")
                    if not key or key in seen_b:
                        continue
                    seen_b.add(key)
                    title = person.get("title", "") or ""
                    if not RELEVANT_RE.search(title) or not geo_ok(person.get("location", "") or "", args.geo):
                        continue
                    prov = {"round": args.round, "provider": "saleleads",
                            "provider_host": "fresh-linkedin-scraper-api.p.rapidapi.com",
                            "endpoint": "/api/v1/search/people", "matched_keyword": kw,
                            "geo_filter": args.geo, "round_keywords": keywords,
                            "company": None, "company_slug": None,
                            "batch_company_slugs": slugs, "batch_saleleads_ids": sids,
                            "via": "source_from_tam_csv:batch"}
                    # company unknown in batch mode → leave leads.current_company for enrichment
                    res = upsert_lead_and_source(db(), person, None, None, prov)
                    if res:
                        if res[1]: new_leads += 1
                        else: existing_leads += 1
                        sources_added += 1
            searched += len(batch)
            if bi % 5 == 0:
                print(f"  ...batch {bi}/{len(batches)} | new_leads={new_leads} existing={existing_leads}", flush=True)
        print(f"\n========== YIELD ({src_label}, batch={N}) — written to MarketBase ==========")
        print(f"  companies resolved/upserted : {len(resolved)}")
        print(f"  companies searched (batched): {searched}")
        print(f"  NEW leads inserted          : {new_leads}")
        print(f"  matched existing leads      : {existing_leads}")
        print(f"  lead_sources rows added     : {sources_added}")
        print(f"  unresolved companies        : {len(unresolved)}")
        return

    for i, p in enumerate(resolved, 1):
        # DB-native resume: skip a company already searched in THIS round.
        if not args.no_resume:
            c = db()
            c.execute("""SELECT 1 FROM lead_sources WHERE source_type=%s AND source_label=%s
                         AND raw_data->>'company_slug'=%s AND raw_data->>'round'=%s LIMIT 1""",
                      (args.source_type, src_label, p["slug"], args.round))
            if c.fetchone():
                skipped += 1
                continue
        seen_co = set()
        kw_totals = {}
        any_capped = False
        co_new = 0
        for kw in keywords:
            ppl, meta = search_people(p["id"], kw, args.max_pages)
            kw_totals[kw] = meta["total"]
            if meta["capped"] or (meta["total"] and meta["returned"] >= cap and meta["total"] > meta["returned"]):
                any_capped = True
            for person in ppl:
                raw_url = person.get("url") or ""
                key = raw_url.split("?")[0].rstrip("/")
                if not key or key in seen_co:
                    continue
                seen_co.add(key)
                title = person.get("title", "") or ""
                loc = person.get("location", "") or ""
                # Only persist relevant (security) + in-geo people as leads.
                if not RELEVANT_RE.search(title) or not geo_ok(loc, args.geo):
                    continue
                matched_kw = kw  # the keyword that surfaced this person first
                url = normalize_linkedin_url(raw_url)
                if not url:
                    continue
                c = db()
                url = resolve_canonical_url(c, url, urn_hint=linkedin_urn(url))
                c.execute("""
                    INSERT INTO leads (linkedin_url, linkedin_urn, name, headline,
                                       current_title, current_company, current_company_url, city)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (linkedin_url) DO UPDATE SET
                        name            = COALESCE(leads.name,            EXCLUDED.name),
                        headline        = COALESCE(leads.headline,        EXCLUDED.headline),
                        current_title   = COALESCE(leads.current_title,   EXCLUDED.current_title),
                        current_company = COALESCE(leads.current_company, EXCLUDED.current_company),
                        current_company_url = COALESCE(leads.current_company_url, EXCLUDED.current_company_url),
                        city            = COALESCE(leads.city,            EXCLUDED.city),
                        linkedin_urn    = COALESCE(leads.linkedin_urn,    EXCLUDED.linkedin_urn),
                        updated_at      = now()
                    RETURNING id, (xmax = 0) AS inserted
                """, (url, linkedin_urn(url), person.get("name") or None, title or None,
                      title or None, p["name"], f"https://www.linkedin.com/company/{p['slug']}/",
                      loc or None))
                lead_id, inserted = c.fetchone()
                if inserted: new_leads += 1; co_new += 1
                else: existing_leads += 1
                # lead_sources — full provenance: provider + the exact keyword that
                # surfaced this person + the list-building round. Guard so each
                # (lead, source_label, round) logs exactly one provenance row.
                c.execute("""INSERT INTO lead_sources (lead_id, source_type, source_label, source_date, raw_data)
                             SELECT %s,%s,%s,CURRENT_DATE,%s::jsonb
                             WHERE NOT EXISTS (SELECT 1 FROM lead_sources
                               WHERE lead_id=%s AND source_type=%s AND source_label=%s
                                 AND raw_data->>'round'=%s)""",
                         (lead_id, args.source_type, src_label,
                          json.dumps({"round": args.round,
                                      "provider": "saleleads",
                                      "provider_host": "fresh-linkedin-scraper-api.p.rapidapi.com",
                                      "endpoint": "/api/v1/search/people",
                                      "matched_keyword": matched_kw,
                                      "geo_filter": args.geo,
                                      "round_keywords": keywords,
                                      "company": p["name"], "company_slug": p["slug"],
                                      "saleleads_id": p["id"], "title": title, "location": loc,
                                      "via": "source_from_tam_csv"}),
                          lead_id, args.source_type, src_label, args.round))
                if c.rowcount:
                    sources_added += 1
        searched += 1
        total_available = sum(v for v in kw_totals.values() if isinstance(v, int))
        untapped = max(0, total_available - len(seen_co))
        if any_capped:
            capped_report.append((p["name"], len(seen_co), total_available, untapped))
        # Stash capacity on the company row (DB-native, queryable later).
        db().execute("""UPDATE companies SET raw_data =
                        jsonb_set(COALESCE(raw_data,'{}'::jsonb), '{tam_people_search}', %s::jsonb, true)
                        WHERE linkedin_slug=%s""",
                     (json.dumps({"source_label": src_label, "max_pages": args.max_pages,
                                  "pulled_distinct": len(seen_co), "relevant_geo_new": co_new,
                                  "total_available_across_keywords": total_available,
                                  "untapped_estimate": untapped, "capped": any_capped,
                                  "keyword_totals": kw_totals}), p["slug"]))
        if i % 20 == 0:
            print(f"  ...{i}/{len(resolved)} companies | new_leads={new_leads} existing={existing_leads} skipped={skipped}", flush=True)

    print(f"\n========== YIELD ({src_label}) — written to MarketBase ==========")
    print(f"  companies resolved/upserted : {len(resolved)}")
    print(f"  companies people-searched   : {searched}  (skipped already-done: {skipped})")
    print(f"  NEW leads inserted          : {new_leads}   <-- net-new relevant+geo leads")
    print(f"  matched existing leads      : {existing_leads}")
    print(f"  lead_sources rows added     : {sources_added}")
    print(f"  unresolved companies        : {len(unresolved)}")
    if unresolved:
        print("   unresolved:", ", ".join(unresolved[:30]) + (" ..." if len(unresolved) > 30 else ""))

    # ---- Untapped-potential report (also persisted per-company in raw_data.tam_people_search) ----
    capped_report.sort(key=lambda r: r[3], reverse=True)
    print(f"\n========== UNTAPPED POTENTIAL (max_pages={args.max_pages}, cap≈{cap}/keyword) ==========")
    print(f"  companies that hit the page cap (more leads available): {len(capped_report)}/{searched}")
    print(f"  {'company':32} {'pulled':>7} {'avail*':>7} {'untapped*':>9}")
    for name, pulled, avail, unt in capped_report[:40]:
        print(f"  {name[:32]:32} {pulled:>7} {avail:>7} {unt:>9}")
    print("  *avail/untapped summed across keywords (overlap inflates slightly) — directional.")


if __name__ == "__main__":
    main()
