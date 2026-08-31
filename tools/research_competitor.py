#!/usr/bin/env python3
"""Research a competitor and write results directly to a client's MarketBase.

- Resolves the company via Saleleads (slug → id). Falls back to LeadMagic
  /company-search by domain when slug-resolution fails.
- UPSERTs `companies`, INSERTs `company_relationships(relationship='competitor')`.
- Runs multiple Saleleads `list-people` keyword passes to find senior execs.
- Filters strictly to the target company_id + a seniority regex.
- UPSERTs each exec into `leads`; INSERTs a `lead_sources` row with
  source_type='find_senior_execs', source_label='<Competitor> (competitor)',
  source_date=today, raw_data=the API record.

No disk writes. Everything in-memory; persistence is exclusively in Postgres.
"""

from __future__ import annoacmens

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

# Make `lib` importable for normalize_linkedin_url
sys.path.insert(0, str(Path(__file__).parent))
from lib import normalize_linkedin_url, load_client_env, database_url, resolve_canonical_url  # noqa: E402

import psycopg2


SALELEADS_CLI = Path.home() / ".claude/tools/saleleads/saleleads_cli.py"
LM_BASE = "https://api.leadmagic.io"

# Keyword passes for senior-exec discovery. Each is a separate Saleleads call.
EXEC_KEYWORDS = [
    "founder", "co-founder", "ceo", "cto", "cmo", "coo", "cfo", "cro", "cpo",
    "chief", "president", "vp", "vice president", "head", "director",
]

# Strict seniority regex applied to each candidate's title clientside.
SENIORITY_RE = re.compile(
    r"\b("
    r"founder|co-?founder|"
    r"chief\b|c[a-z]o\b|"
    r"president|svp\b|evp\b|avp\b|vp\b|vice\s+president|"
    r"head\b|director\b|"
    r"general\s+manager|gm\b|"
    r"owner|principal\b|managing\b"
    r")",
    re.I,
)

# Match the FIRST "at X" / "@ X" in a title, capturing X up to a separator
# (-, |, ;, /, end of line, " - ", " — ", or end of string).
AT_COMPANY_RE = re.compile(
    r"(?:\s+at\s+|\s*@\s*)([^|;/\-—\n]+?)(?:\s*[|;/\-—]|\s*$)",
    re.I,
)


def _norm_for_match(s: str) -> str:
    """Lowercase + collapse non-alphanumeric for fuzzy company-name match."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def title_is_at_target(title: str, target_name: str) -> bool:
    """Returns True if the title is unambiguously at the target company.

    Heuristic:
      - If the title has no `at X` / `@ X` clause → keep (we can't tell from text;
        Saleleads's company_id filter is presumed authoritative).
      - If the FIRST `at X` clause names the target company (fuzzy substring on
        normalized strings) → keep.
      - Otherwise → drop (the title contradicts the company filter — usually
        a board member, investor, or wrong-company match leaking through).
    """
    if not title:
        return True
    m = AT_COMPANY_RE.search(title)
    if not m:
        return True
    company_in_title = _norm_for_match(m.group(1))
    target = _norm_for_match(target_name)
    return target in company_in_title or company_in_title in target


# ── Saleleads + LeadMagic resolution ──────────────────────────────────────────

def saleleads_company(slug_or_id: str, retries: int = 3, backoff: int = 15) -> dict[str, Any] | None:
    """Resolve a company via Saleleads. Tries id-as-int first, then as universal name.
    Retries on transient upstream 429s (`status 429: Request denied`) with backoff."""
    args = ["--id", slug_or_id] if slug_or_id.isdigit() else ["--name", slug_or_id]
    for attempt in range(retries):
        try:
            out = subprocess.run(
                ["python3", str(SALELEADS_CLI), "company-profile", *args],
                capture_output=True, text=True, timeout=30,
            )
            d = json.loads(out.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            time.sleep(backoff * (attempt + 1))
            continue
        if d.get("success"):
            return d.get("data")
        msg = (d.get("message") or "").lower()
        if "429" in msg or "rate" in msg or "denied" in msg:
            time.sleep(backoff * (attempt + 1))
            continue
        # Hard failure (404, etc.) — don't retry
        return None
    return None


def leadmagic_company_by_domain(domain: str) -> dict[str, Any] | None:
    key = os.environ.get("LEADMAGIC_API_KEY")
    if not key:
        return None
    req = urllib.request.Request(
        f"{LM_BASE}/company-search",
        data=json.dumps({"company_domain": domain}).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    if d.get("companyId"):
        return d
    return None


def leadmagic_profile(url: str) -> dict[str, Any] | None:
    """Resolve a LinkedIn person profile URL via LeadMagic /profile-search."""
    key = os.environ.get("LEADMAGIC_API_KEY")
    if not key:
        return None
    req = urllib.request.Request(
        f"{LM_BASE}/profile-search",
        data=json.dumps({"profile_url": url}).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
    except Exception:
        return None
    # Heuristic: a valid response has first_name OR full_name OR company_name.
    if d.get("first_name") or d.get("full_name") or d.get("company_name"):
        return d
    return None


def resolve_competitor(token: str) -> dict[str, Any] | None:
    """Resolve a competitor to a canonical company record.

    Token may be: numeric id, universal slug, full LinkedIn URL, or website domain.
    Returns dict with id, name, slug, linkedin_url, website, employee_count, employee_range.
    Tries Saleleads first; falls back to LeadMagic /company-search by domain.
    """
    # Parse the input
    t = token.strip()

    slug = None
    linkedin_url = None
    if t.isdigit():
        slug_or_id = t
    elif "linkedin.com/company/" in t.lower():
        m = re.search(r"linkedin\.com/company/([^/?#]+)", t.lower())
        slug = m.group(1) if m else None
        slug_or_id = slug
        linkedin_url = t
    elif "." in t and " " not in t:  # looks like a domain
        slug_or_id = None
        domain = t
    else:
        slug_or_id = t  # try as slug

    # Try Saleleads
    if slug_or_id:
        c = saleleads_company(slug_or_id)
        if c:
            return {
                "id":              str(c.get("id") or ""),
                "name":            c.get("name") or "",
                "linkedin_slug":   c.get("universal_name") or slug or "",
                "linkedin_url":    c.get("linkedin_url") or linkedin_url or "",
                "website":         c.get("website_url") or "",
                "employee_count":  c.get("employee_count"),
                "employee_range":  (
                    f"{c['employee_count_range']['start']}-{c['employee_count_range']['end']}"
                    if c.get("employee_count_range") else None
                ),
                "industry":        ", ".join(c.get("industries") or []) or None,
                "raw":             c,
                "source":          "saleleads",
            }

    # Fallback: LeadMagic by domain
    domain_to_try = None
    if "." in t and " " not in t:
        domain_to_try = t.replace("https://", "").replace("http://", "").split("/")[0]
    if domain_to_try:
        lm = leadmagic_company_by_domain(domain_to_try)
        if lm:
            # Use Saleleads to enrich now that we have the company ID
            c = saleleads_company(str(lm["companyId"]))
            if c:
                return {
                    "id":              str(c.get("id") or lm["companyId"]),
                    "name":            c.get("name") or lm.get("companyName") or "",
                    "linkedin_slug":   c.get("universal_name") or "",
                    "linkedin_url":    c.get("linkedin_url") or "",
                    "website":         c.get("website_url") or lm.get("websiteUrl") or domain_to_try,
                    "employee_count":  c.get("employee_count") or lm.get("employeeCount"),
                    "employee_range":  None,
                    "industry":        lm.get("industry"),
                    "raw":             {"saleleads": c, "leadmagic": lm},
                    "source":          "saleleads+leadmagic",
                }
            # Saleleads couldn't ratify; use LeadMagic only
            return {
                "id":              str(lm["companyId"]),
                "name":            lm.get("companyName") or "",
                "linkedin_slug":   "",
                "linkedin_url":    f"https://www.linkedin.com/company/{lm.get('companyName','').lower().replace(' ', '-')}/",
                "website":         lm.get("websiteUrl") or domain_to_try,
                "employee_count":  lm.get("employeeCount"),
                "employee_range":  None,
                "industry":        lm.get("industry"),
                "raw":             {"leadmagic": lm},
                "source":          "leadmagic",
            }

    return None


# ── Senior exec discovery ────────────────────────────────────────────────────

def saleleads_list_people(keyword: str, company_id: str, max_pages: int = 3) -> list[dict]:
    """Returns list of people dicts (name, title, url, urn, public_id, location)."""
    try:
        out = subprocess.run(
            ["python3", str(SALELEADS_CLI), "list-people",
             "--keyword", keyword, "--company-id", company_id,
             "--max-pages", str(max_pages)],
            capture_output=True, text=True, timeout=120,
        )
        d = json.loads(out.stdout)
        return d.get("people") or []
    except Exception:
        return []


def find_senior_execs(company_id: str, company_name: str,
                      product_keyword: str | None = None,
                      keywords: list[str] | None = None,
                      title_regex: str | None = None) -> tuple[list[dict], dict[str, int]]:
    """Run all keyword passes, dedupe by URN, filter by seniority regex + 'at target' check.

    For showcase mode, `company_id` is the PARENT company's id, `company_name` is
    the parent's name (for the at-target check), and `product_keyword` is the
    showcase product name (e.g. "Prisma", "Cortex"). Survivors must additionally
    have the product keyword in their title.

    `keywords` overrides the default EXEC_KEYWORDS sweep (e.g. a sales-only set).
    `title_regex` is an optional RAW (un-escaped) regex that, when set, every
    survivor's title must additionally match — use it to constrain the sweep to a
    function, e.g. sales/revenue. It composes with `product_keyword`.

    Returns (kept_execs, drop_stats) where drop_stats counts each drop reason.
    """
    by_urn: dict[str, dict] = {}
    for kw in (keywords or EXEC_KEYWORDS):
        for p in saleleads_list_people(kw, company_id):
            urn = p.get("urn") or p.get("public_id") or p.get("url")
            if not urn:
                continue
            # Keep richest record (longest title)
            if urn not in by_urn or len(p.get("title") or "") > len(by_urn[urn].get("title") or ""):
                p["_matched_keywords"] = sorted(set((by_urn.get(urn, {}).get("_matched_keywords") or []) + [kw]))
                by_urn[urn] = p
        time.sleep(0.2)

    kw_re = re.compile(rf"\b{re.escape(product_keyword)}\b", re.I) if product_keyword else None
    title_re = re.compile(title_regex, re.I) if title_regex else None
    kept: list[dict] = []
    drops = {"no_title": 0, "not_senior": 0, "at_other_company": 0,
             "no_product_keyword": 0, "no_title_regex": 0}
    for p in by_urn.values():
        title = p.get("title") or ""
        if not title.strip():
            drops["no_title"] += 1
            continue
        if not SENIORITY_RE.search(title):
            drops["not_senior"] += 1
            continue
        if not title_is_at_target(title, company_name):
            drops["at_other_company"] += 1
            continue
        if kw_re and not kw_re.search(title):
            drops["no_product_keyword"] += 1
            continue
        if title_re and not title_re.search(title):
            drops["no_title_regex"] += 1
            continue
        kept.append(p)
    return kept, drops


# ── DB persistence ────────────────────────────────────────────────────────────

def upsert_company(cur, c: dict, source: str) -> str:
    """UPSERT into companies. Returns the company UUID."""
    slug = c["linkedin_slug"] or c["id"] or c["name"].lower().replace(" ", "-")
    cur.execute("""
        INSERT INTO companies (linkedin_slug, linkedin_url, name, website, industry,
                               employee_count, employee_range, saleleads_id,
                               is_showcase, parent_linkedin_slug,
                               raw_data, size_fetched_at)
        VALUES (%(slug)s, %(url)s, %(name)s, %(web)s, %(ind)s,
                %(ec)s, %(er)s, %(slid)s, %(showcase)s, %(parent)s,
                %(raw)s::jsonb, now())
        ON CONFLICT (linkedin_slug) DO UPDATE SET
            linkedin_url   = COALESCE(EXCLUDED.linkedin_url,   companies.linkedin_url),
            name           = COALESCE(EXCLUDED.name,           companies.name),
            website        = COALESCE(EXCLUDED.website,        companies.website),
            industry       = COALESCE(EXCLUDED.industry,       companies.industry),
            employee_count = COALESCE(EXCLUDED.employee_count, companies.employee_count),
            employee_range = COALESCE(EXCLUDED.employee_range, companies.employee_range),
            saleleads_id   = COALESCE(EXCLUDED.saleleads_id,   companies.saleleads_id),
            is_showcase            = EXCLUDED.is_showcase,
            parent_linkedin_slug   = EXCLUDED.parent_linkedin_slug,
            raw_data       = EXCLUDED.raw_data,
            size_fetched_at = now()
        RETURNING id
    """, {
        "slug": slug, "url": c.get("linkedin_url"), "name": c.get("name"),
        "web": c.get("website"), "ind": c.get("industry"),
        "ec": c.get("employee_count"), "er": c.get("employee_range"),
        "slid": c.get("id"),
        "showcase": bool(c.get("is_showcase")),
        "parent": c.get("parent_linkedin_slug"),
        "raw": json.dumps(c.get("raw") or {}),
    })
    return cur.fetchone()[0]


def upsert_relationship(cur, company_uuid: str, relationship: str, scope: str, notes: str | None) -> bool:
    """INSERT or UPDATE a (company, relationship) row. Returns True if newly inserted."""
    cur.execute("""
        INSERT INTO company_relationships (company_id, relationship, scope, source, notes)
        VALUES (%s, %s, %s, 'marketbase-research-competitor', %s)
        ON CONFLICT (company_id, relationship) DO UPDATE SET
            scope = COALESCE(EXCLUDED.scope, company_relationships.scope),
            notes = COALESCE(EXCLUDED.notes, company_relationships.notes)
        RETURNING (xmax = 0) AS inserted
    """, (company_uuid, relationship, scope, notes))
    return bool(cur.fetchone()[0])


def existing_execs_count(cur, source_label: str) -> int:
    cur.execute(
        "SELECT count(*) FROM lead_sources WHERE source_type = 'find_senior_execs' AND source_label = %s",
        (source_label,),
    )
    return cur.fetchone()[0]


def upsert_lead(cur, p: dict, company_name: str, company_url: str) -> str:
    """UPSERT lead by canonical linkedin_url. Returns lead UUID."""
    li_url = resolve_canonical_url(cur, normalize_linkedin_url(p.get("url") or ""),
                                   urn_hint=p.get("urn"))
    name = (p.get("name") or "").strip()
    title = (p.get("title") or "").strip()
    cur.execute("""
        INSERT INTO leads (linkedin_url, linkedin_urn, public_id, name, current_title,
                           current_company, current_company_url, last_enriched_at)
        VALUES (%(url)s, %(urn)s, %(pid)s, %(name)s, %(title)s, %(co)s, %(cu)s, now())
        ON CONFLICT (linkedin_url) DO UPDATE SET
            linkedin_urn        = COALESCE(EXCLUDED.linkedin_urn,        leads.linkedin_urn),
            public_id           = COALESCE(EXCLUDED.public_id,           leads.public_id),
            name                = COALESCE(leads.name,                   EXCLUDED.name),
            current_title       = COALESCE(EXCLUDED.current_title,       leads.current_title),
            current_company     = COALESCE(EXCLUDED.current_company,     leads.current_company),
            current_company_url = COALESCE(EXCLUDED.current_company_url, leads.current_company_url),
            last_enriched_at    = now(),
            updated_at          = now()
        RETURNING id
    """, {
        "url": li_url,
        "urn": p.get("urn"),
        "pid": p.get("public_id"),
        "name": name,
        "title": title,
        "co": company_name,
        "cu": company_url,
    })
    return cur.fetchone()[0]


def insert_lead_source(cur, lead_uuid: str, source_label: str, raw: dict) -> None:
    cur.execute("""
        INSERT INTO lead_sources (lead_id, source_type, source_label, source_date, raw_data)
        VALUES (%s, 'find_senior_execs', %s, %s, %s::jsonb)
    """, (lead_uuid, source_label, date.today(), json.dumps(raw)))


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _leadmagic_to_saleleads_shape(li_url: str, lm: dict) -> dict:
    """Reshape a LeadMagic /profile-search response into the same shape used by
    the Saleleads `list-people` records, so the same upsert path works."""
    first = lm.get("first_name") or ""
    last = lm.get("last_name") or ""
    full = lm.get("full_name") or f"{first} {last}".strip()
    title = (lm.get("professional_title")
             or (lm.get("current_position") or {}).get("position_title")
             or "")
    return {
        "name": full or None,
        "title": title or None,
        "url": li_url,
        "urn": None,
        "public_id": (li_url.rstrip("/").split("/")[-1] if li_url else None),
        "location": lm.get("location"),
        "_leadmagic_raw": lm,
    }


def ingest_execs_from_urls(cur, urls: list[str], c: dict, source_label: str) -> tuple[int, int, list[str]]:
    """Enrich each URL via LeadMagic /profile-search and UPSERT.
    Returns (n_ingested, n_skipped, warnings)."""
    n_ok, n_skipped = 0, 0
    warnings: list[str] = []
    target_co = _norm_for_match(c["name"])
    for url in urls:
        url = url.strip()
        if not url:
            continue
        lm = leadmagic_profile(url)
        if not lm:
            warnings.append(f"profile not found: {url}")
            n_skipped += 1
            continue
        p = _leadmagic_to_saleleads_shape(url, lm)
        lm_company = lm.get("company_name") or ""
        if lm_company and target_co and _norm_for_match(lm_company) != target_co \
                and target_co not in _norm_for_match(lm_company) \
                and _norm_for_match(lm_company) not in target_co:
            warnings.append(f"  ⚠ {p['name']}: LeadMagic says current company = {lm_company!r}, not {c['name']!r} (still ingesting)")
        lead_uuid = upsert_lead(cur, p, c["name"], c["linkedin_url"])
        insert_lead_source(cur, lead_uuid, source_label, p)
        n_ok += 1
        print(f"  + {p['name'] or '?':<26} | {(p['title'] or '')[:60]:<60} | {url}")
    return n_ok, n_skipped, warnings


def lookup_existing_company(cur, token: str) -> dict | None:
    """Best-effort lookup by linkedin_slug or numeric saleleads_id of an already-ingested company.
    Returns a dict in the same shape as resolve_competitor(), or None."""
    if token.isdigit():
        cur.execute("SELECT id, linkedin_slug, linkedin_url, name, website, industry, "
                    "employee_count, employee_range, saleleads_id "
                    "FROM companies WHERE saleleads_id=%s", (token,))
    else:
        # Normalize: try as slug; also pull slug from URL if applicable
        slug = token
        if "linkedin.com/company/" in token.lower():
            m = re.search(r"linkedin\.com/company/([^/?#]+)", token.lower())
            if m: slug = m.group(1)
        cur.execute("SELECT id, linkedin_slug, linkedin_url, name, website, industry, "
                    "employee_count, employee_range, saleleads_id "
                    "FROM companies WHERE linkedin_slug=%s", (slug,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id":             row[8] or "",        # saleleads_id
        "name":           row[3],
        "linkedin_slug":  row[1],
        "linkedin_url":   row[2],
        "website":        row[4],
        "industry":       row[5],
        "employee_count": row[6],
        "employee_range": row[7],
        "raw":            {},
        "source":         "existing_db_row",
        "_company_uuid":  row[0],
    }


def run(client: str, competitor: str, relationship: str, scope: str | None,
        refresh_execs: bool, exec_urls: list[str] | None = None,
        showcase_parent: str | None = None,
        product_keyword: str | None = None,
        exec_keywords: list[str] | None = None,
        title_regex: str | None = None,
        no_execs: bool = False) -> dict:
    load_client_env(client)
    url = database_url(client)

    print(f"\n=== {competitor} → {client} ===")

    # When --exec is provided AND the company already exists in this client's MarketBase,
    # skip the Saleleads/LeadMagic resolution entirely. Saves API calls + dodges
    # transient 429s. Falls back to live resolution if not yet in the DB.
    c = None
    if exec_urls:
        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                c = lookup_existing_company(cur, competitor)
        if c:
            print(f"  ✓ matched existing DB row: {c['name']} (slug={c['linkedin_slug']}, "
                  f"emp={c['employee_count']}) — skipping live resolution")

    if not c:
        c = resolve_competitor(competitor)
        if not c:
            print(f"  ✗ could not resolve {competitor!r} via Saleleads or LeadMagic")
            return {"status": "RESOLUTION_FAILED"}
        print(f"  ✓ resolved: {c['name']} (id={c['id']}, slug={c['linkedin_slug']}, "
              f"emp={c['employee_count']}) via {c['source']}")

    # Detect showcase pages by URL pattern. If the LinkedIn URL contains
    # `/showcase/`, this is a showcase, and we require both --showcase-parent
    # and --product-keyword so we know whose employees to filter.
    is_showcase = "/showcase/" in (c.get("linkedin_url") or "")
    c["is_showcase"] = is_showcase
    if is_showcase:
        if not showcase_parent or not product_keyword:
            print(f"  ✗ {c['name']} is a showcase page — requires --showcase-parent <parent-slug> "
                  f"and --product-keyword <kw>")
            return {"status": "MISSING_SHOWCASE_PARAMS"}
        c["parent_linkedin_slug"] = showcase_parent
        # Resolve parent now so we have its saleleads_id for the keyword sweep.
        parent = resolve_competitor(showcase_parent)
        if not parent:
            print(f"  ✗ could not resolve showcase parent {showcase_parent!r}")
            return {"status": "PARENT_RESOLUTION_FAILED"}
        print(f"  · showcase parent: {parent['name']} (id={parent['id']}, slug={parent['linkedin_slug']}, "
              f"emp={parent['employee_count']})")
        c["_parent_resolved"] = parent
    else:
        c["parent_linkedin_slug"] = None
        if showcase_parent or product_keyword:
            print(f"  · ignoring --showcase-parent/--product-keyword (target is not a showcase page)")

    source_label = f"{c['name']} ({relationship})"

    with psycopg2.connect(url) as conn:
        with conn.cursor() as cur:
            company_uuid = upsert_company(cur, c, c["source"])
            newly_flagged = upsert_relationship(cur, company_uuid, relationship, scope, None)
            scope_msg = f", scope={scope}" if scope else ""
            print(f"  {'+ flagged' if newly_flagged else '· already flagged'} as {relationship}"
                  f"{scope_msg} (company_uuid={company_uuid})")

            # Flag-only mode: the company row + relationship are all we want.
            # Used when a company is landscape context (e.g. an adjacent vendor
            # we probe for SSO tenancy) whose leadership we have no reason to
            # ingest — skips the whole paid Saleleads keyword sweep.
            if no_execs:
                conn.commit()
                return {"status": "FLAGGED_ONLY", "company_uuid": company_uuid}

            n_existing = existing_execs_count(cur, source_label)
            if n_existing and not refresh_execs and not exec_urls:
                print(f"  · {n_existing} senior-exec source rows already exist for "
                      f"{source_label!r}; skipping. Use --refresh-execs to redo.")
                conn.commit()
                return {"status": "SKIPPED_EXISTS", "company_uuid": company_uuid,
                        "existing_execs": n_existing}

            # MODE A: explicit URL list — bypass Saleleads keyword sweep entirely.
            if exec_urls:
                print(f"  · ingesting {len(exec_urls)} explicit profile URL(s) via LeadMagic /profile-search…")
                n_ok, n_skipped, warnings = ingest_execs_from_urls(cur, exec_urls, c, source_label)
                for w in warnings:
                    print(f"    {w}")
                conn.commit()
                print(f"  ✓ upserted {n_ok} leads ({n_skipped} skipped)")
                return {"status": "OK", "company_uuid": company_uuid, "exec_count": n_ok,
                        "skipped": n_skipped, "dropped_at_other_company": 0}

            # MODE B: keyword sweep via Saleleads.
            # For showcases, search the PARENT's employees but filter by product keyword.
            if c.get("is_showcase"):
                parent = c["_parent_resolved"]
                # Also upsert the parent so we have a clean row to reference.
                parent_uuid = upsert_company(cur, {**parent,
                                                   "is_showcase": False,
                                                   "parent_linkedin_slug": None}, parent["source"])
                print(f"  · running {len(EXEC_KEYWORDS)} keyword passes against PARENT "
                      f"{parent['name']!r} filtering by product keyword {product_keyword!r}…")
                execs, drops = find_senior_execs(parent["id"], parent["name"],
                                                 product_keyword=product_keyword,
                                                 keywords=exec_keywords,
                                                 title_regex=title_regex)
                print(f"  ✓ {len(execs)} product-leaders kept "
                      f"(dropped {drops['no_title']} no_title, "
                      f"{drops['not_senior']} not_senior, "
                      f"{drops['at_other_company']} at_other_company, "
                      f"{drops['no_product_keyword']} no_product_keyword, "
                      f"{drops['no_title_regex']} no_title_regex)")
            else:
                kw_set = exec_keywords or EXEC_KEYWORDS
                print(f"  · running {len(kw_set)} keyword passes via Saleleads"
                      f"{' (sales-scoped)' if title_regex else ''}…")
                execs, drops = find_senior_execs(c["id"], c["name"],
                                                 keywords=exec_keywords,
                                                 title_regex=title_regex)
                print(f"  ✓ {len(execs)} senior execs kept "
                      f"(dropped {drops['no_title']} no_title, "
                      f"{drops['not_senior']} not_senior, "
                      f"{drops['at_other_company']} at_other_company, "
                      f"{drops['no_title_regex']} no_title_regex)")

            n_upserts = 0
            for p in execs:
                if not p.get("url"):
                    continue
                lead_uuid = upsert_lead(cur, p, c["name"], c["linkedin_url"])
                insert_lead_source(cur, lead_uuid, source_label, p)
                n_upserts += 1
            conn.commit()
            print(f"  ✓ upserted {n_upserts} leads + {n_upserts} lead_sources rows")

    return {"status": "OK", "company_uuid": company_uuid, "exec_count": len(execs),
            "dropped_at_other_company": drops["at_other_company"]}


def main():
    ap = argparse.ArgumentParser(description="Research a company (competitor / self / customer / etc.) and write to a client's MarketBase.")
    ap.add_argument("--client", required=True, help="Client name (e.g. Acme)")
    ap.add_argument("--competitor", action="append", required=True,
                    help="Company identifier (slug, id, LinkedIn URL, or domain). Repeatable.")
    ap.add_argument("--relationship", default="competitor",
                    help=("Value for company_relationships.relationship. Canonical: "
                          "competitor, security_vendor, bought_competitor_product, self, "
                          "customer, partner, vendor. See CONVENTIONS.md for which values "
                          "disqualify employees. Default: competitor."))
    ap.add_argument("--scope",
                    help="Optional scope (free text). For competitors: direct|adjacent|aspirational. Omit for self.")
    ap.add_argument("--refresh-execs", action="store_true",
                    help="Re-fetch senior execs even if rows already exist for this label.")
    ap.add_argument("--no-execs", action="store_true",
                    help="Resolve + upsert the company and write the relationship row, then STOP. "
                         "Skips the paid Saleleads senior-exec sweep entirely. Use when you only "
                         "want the company on the map (landscape context, SSO-probe targets) and "
                         "have no reason to ingest its leadership.")
    ap.add_argument("--exec", dest="exec_urls", action="append", default=[],
                    help="Specific LinkedIn profile URL to enrich + ingest as an exec for this company. "
                         "Repeatable. When provided, bypasses the Saleleads keyword sweep entirely "
                         "(useful for small companies where Saleleads has no employee coverage — typically self).")
    ap.add_argument("--showcase-parent",
                    help="LinkedIn slug of the parent company for a showcase page "
                         "(e.g. 'palo-alto-networks' for the Prisma showcase). REQUIRED when "
                         "the competitor URL is a /showcase/ page. Ignored otherwise.")
    ap.add_argument("--product-keyword",
                    help="Product name keyword (e.g. 'Prisma', 'Cortex'). When researching "
                         "a showcase page, senior-exec discovery runs against the parent's "
                         "employees and survivors must have this keyword in their title. "
                         "REQUIRED when --showcase-parent is set.")
    ap.add_argument("--exec-keywords",
                    help="Comma-separated keyword set to OVERRIDE the default all-functions "
                         "EXEC_KEYWORDS sweep (e.g. a sales-only set). Each becomes one "
                         "Saleleads list-people pass.")
    ap.add_argument("--title-regex",
                    help="Optional RAW (un-escaped) case-insensitive regex every survivor's "
                         "title must additionally match — use to constrain the sweep to a "
                         "function, e.g. '(sales|revenue|commercial|gtm|go-to-market)'.")
    args = ap.parse_args()

    exec_keywords = ([k.strip() for k in args.exec_keywords.split(",") if k.strip()]
                     if args.exec_keywords else None)

    results = []
    for comp in args.competitor:
        res = run(args.client, comp, args.relationship, args.scope, args.refresh_execs,
                  exec_urls=args.exec_urls or None,
                  showcase_parent=args.showcase_parent,
                  product_keyword=args.product_keyword,
                  exec_keywords=exec_keywords,
                  title_regex=args.title_regex,
                  no_execs=args.no_execs)
        results.append({"competitor": comp, **res})

    print("\n=== summary ===")
    for r in results:
        print(f"  {r['competitor']:<30} | {r['status']:<20} | execs={r.get('exec_count', '-')} | "
              f"dropped_at_other_company={r.get('dropped_at_other_company', '-')}")


if __name__ == "__main__":
    main()
