#!/usr/bin/env python3
"""marketbase-discover-candidates-via-referrer (deterministic pre-step)

Given a referrer in a client's MarketBase and a hint at the team the referrer
pointed us at, surface candidates from two deterministic sources:

  1. MarketBase leads at the same company matching title keywords
  2. Apollo `mixed_people/api_search` (org domain + title keywords + seniorities)

For Apollo's free-tier obfuscated names (e.g. "Yan Ko***s"), the agent runs
WebSearch separately to resolve full names + LinkedIn URLs — this script
ONLY produces the deterministic deduplicated candidate list with a
`needs_name_resolution: yes/no` flag.

Output is a CSV with one row per candidate:

  source            MarketBase / apollo
  first_name
  last_name         (full from MarketBase; obfuscated from apollo)
  full_name         (only when fully known)
  title
  company
  linkedin_url      (MarketBase URL; blank for apollo until web search resolves)
  apollo_id         (for apollo rows)
  needs_name_resolution    yes for apollo, no for MarketBase
  notes             dedup hints, e.g. "matches MarketBase row by first+last initial"

Usage:
    python3 discover_candidates_via_referrer.py \\
        --client Acme \\
        --referrer-url https://www.linkedin.com/in/<...> \\
        --title-keywords "vulnerability,patch,cloud security,security operations,CISO,head of security,director security" \\
        --seniorities "director,vp,head,senior_director,c_suite" \\
        --output /tmp/rbc_candidates.csv

Apollo API keys are pulled from environment (`APOLLO_API_KEY*`).
The skill (marketbase-discover-candidates-via-referrer/SKILL.md) wraps this.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, normalize_linkedin_url


# Apollo API — keys rotated on 429 / exhausted credit messages
APOLLO_SEARCH_URL = "https://api.apollo.io/v1/mixed_people/api_search"

from lib import secrets
# Resolved from Infisical / env. Set APOLLO_API_KEYS as a comma-separated pool.
APOLLO_KEYS = [{"key": k, "account": f"key{i+1}"} for i, k in enumerate(secrets.get_list("APOLLO_API_KEYS"))]


def company_to_domain(company: str) -> str | None:
    """Best-effort guess at a company's primary domain.

    Apollo's `q_organization_domains_list` is strict — `rbc.com` works,
    `RBC` does not. The agent should usually pass `--org-domain` explicitly.
    Fall back to a naive heuristic only if not given.
    """
    if not company:
        return None
    # Naive: lowercase + strip whitespace, append .com
    # NOT reliable — use --org-domain when the agent knows it.
    return company.lower().replace(" ", "") + ".com"


def apollo_search(org_domain: str, titles: list[str], seniorities: list[str],
                  per_page: int = 50) -> list[dict]:
    """Call Apollo. Rotate keys on 429 / 'credit' errors."""
    body = {
        "q_organization_domains_list": [org_domain],
        "person_titles": titles,
        "person_seniorities": seniorities,
        "page": 1,
        "per_page": per_page,
    }
    body_json = json.dumps(body).encode()

    for idx, key_info in enumerate(APOLLO_KEYS):
        req = urllib.request.Request(
            APOLLO_SEARCH_URL,
            data=body_json,
            headers={
                "X-Api-Key": key_info["key"],
                "User-Agent": "curl/7.88.1",
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            payload = e.read().decode()
            print(f"  Apollo HTTP {e.code} on key #{idx}: {payload[:200]}",
                  file=sys.stderr)
            if "credit" in payload.lower() or e.code == 429:
                continue
            raise
        if resp.get("error"):
            msg = resp.get("error", "")
            print(f"  Apollo API error on key #{idx}: {msg}", file=sys.stderr)
            if "credit" in msg.lower() or "rate" in msg.lower():
                continue
            return []
        return resp.get("people") or []

    print("  All Apollo keys exhausted or errored.", file=sys.stderr)
    return []


def query_gtmdb_candidates(cur, company_terms: list[str], title_kws: list[str],
                           exclude_url: str | None) -> list[dict]:
    """Match leads where current_company ILIKE one of company_terms AND
    headline/title ILIKE one of title_kws.
    """
    company_clauses = " OR ".join(
        ["l.current_company ILIKE %s" for _ in company_terms]
    )
    title_clauses = " OR ".join(
        [f"l.headline ILIKE %s OR l.current_title ILIKE %s"
         for _ in title_kws]
    )

    params = []
    for c in company_terms:
        params.append(f"%{c}%")
    for k in title_kws:
        params.append(f"%{k}%")
        params.append(f"%{k}%")

    sql = f"""
        SELECT l.linkedin_url, l.name, l.current_title, l.current_company, l.headline
        FROM leads l
        WHERE ({company_clauses})
          AND ({title_clauses})
    """
    if exclude_url:
        sql += " AND l.linkedin_url <> %s"
        params.append(exclude_url)

    cur.execute(sql, tuple(params))
    rows = []
    for r in cur.fetchall():
        rows.append({
            "linkedin_url": r[0],
            "name": r[1] or "",
            "title": r[2] or "",
            "company": r[3] or "",
            "headline": r[4] or "",
        })
    return rows


def parse_full_name(name: str) -> tuple[str, str]:
    """Split full name into (first, last). Crude but workable."""
    parts = (name or "").strip().split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], " ".join(parts[1:]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--client", required=True)
    ap.add_argument("--referrer-url", required=True,
                    help="LinkedIn URL of the referrer; used to identify the target company.")
    ap.add_argument("--org-domain",
                    help="Authoritative company domain for Apollo search "
                         "(e.g. rbc.com). If omitted, the script guesses from the "
                         "referrer's `current_company` — unreliable.")
    ap.add_argument("--org-aliases",
                    help="Comma-separated company name fragments to match in MarketBase "
                         "(e.g. 'RBC,Royal Bank'). Defaults to the referrer's "
                         "current_company verbatim.")
    ap.add_argument("--title-keywords", required=True,
                    help="Comma-separated title/headline keywords (e.g. "
                         "'vulnerability,patch,cloud security,CISO').")
    ap.add_argument("--seniorities",
                    default="director,vp,head,senior_director,c_suite",
                    help="Comma-separated Apollo seniority codes (default: "
                         "director,vp,head,senior_director,c_suite).")
    ap.add_argument("--per-page", type=int, default=50,
                    help="Apollo per_page (default 50).")
    ap.add_argument("--output", required=True,
                    help="CSV output path.")
    args = ap.parse_args()

    title_kws = [k.strip() for k in args.title_keywords.split(",") if k.strip()]
    seniorities = [s.strip() for s in args.seniorities.split(",") if s.strip()]

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            # 1. Look up referrer + their company
            cur.execute("""
                SELECT name, current_company
                FROM leads WHERE linkedin_url = %s
            """, (normalize_linkedin_url(args.referrer_url),))
            row = cur.fetchone()
            if not row:
                sys.exit(f"Referrer not found in MarketBase: {args.referrer_url}")
            referrer_name, referrer_company = row
            if not referrer_company:
                sys.exit(f"Referrer {referrer_name!r} has no current_company; "
                         f"pass --org-aliases explicitly.")

            print(f"Referrer: {referrer_name} ({referrer_company})", flush=True)

            # 2. MarketBase match
            company_aliases = (
                [a.strip() for a in args.org_aliases.split(",")]
                if args.org_aliases else [referrer_company]
            )
            gtmdb_hits = query_gtmdb_candidates(
                cur, company_aliases, title_kws,
                exclude_url=normalize_linkedin_url(args.referrer_url),
            )
            print(f"  MarketBase matches: {len(gtmdb_hits)}", flush=True)

            # 3. Apollo
            org_domain = args.org_domain or company_to_domain(referrer_company)
            if not args.org_domain:
                print(f"  ⚠ --org-domain not given, guessed {org_domain!r} from "
                      f"company name — may be wrong.", flush=True)
            apollo_hits = apollo_search(org_domain, title_kws, seniorities,
                                        per_page=args.per_page)
            print(f"  Apollo matches: {len(apollo_hits)}", flush=True)

            # 4. Build dedup index from MarketBase
            # Key by (first_name_lower, last_initial_lower) when last name in MarketBase
            gtmdb_by_first_last_init: dict[tuple[str, str], dict] = {}
            for h in gtmdb_hits:
                first, last = parse_full_name(h["name"])
                if first and last:
                    key = (first.lower(), last[0].lower())
                    gtmdb_by_first_last_init[key] = h

            # 5. Build output rows
            out_rows: list[dict] = []
            # MarketBase rows
            for h in gtmdb_hits:
                first, last = parse_full_name(h["name"])
                out_rows.append({
                    "source": "MarketBase",
                    "first_name": first,
                    "last_name": last,
                    "full_name": h["name"],
                    "title": h["title"],
                    "company": h["company"],
                    "linkedin_url": h["linkedin_url"],
                    "apollo_id": "",
                    "needs_name_resolution": "no",
                    "notes": "",
                })

            # Apollo rows
            for p in apollo_hits:
                first = p.get("first_name", "")
                last_obf = p.get("last_name_obfuscated") or ""
                title = p.get("title", "")
                org = (p.get("organization") or {}).get("name") or ""
                apollo_id = p.get("id", "")

                # Dedup against MarketBase by first + last initial
                dedup_note = ""
                if last_obf:
                    last_initial = last_obf[0].lower()
                    if (first.lower(), last_initial) in gtmdb_by_first_last_init:
                        match = gtmdb_by_first_last_init[(first.lower(), last_initial)]
                        dedup_note = f"likely same as MarketBase row {match['name']!r}"

                out_rows.append({
                    "source": "apollo",
                    "first_name": first,
                    "last_name": last_obf,
                    "full_name": "",  # unresolved
                    "title": title,
                    "company": org,
                    "linkedin_url": "",
                    "apollo_id": apollo_id,
                    "needs_name_resolution": "yes",
                    "notes": dedup_note,
                })

    # 6. Write CSV
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "source", "first_name", "last_name", "full_name",
            "title", "company", "linkedin_url", "apollo_id",
            "needs_name_resolution", "notes",
        ])
        w.writeheader()
        w.writerows(out_rows)
    print(f"\nWrote {len(out_rows)} rows → {args.output}", flush=True)
    print(f"  MarketBase (full names): "
          f"{sum(1 for r in out_rows if r['source'] == 'MarketBase')}", flush=True)
    print(f"  apollo (need name resolution): "
          f"{sum(1 for r in out_rows if r['needs_name_resolution'] == 'yes')}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
