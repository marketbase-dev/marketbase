#!/usr/bin/env python3
"""fill_company_size — Saleleads /company/profile backfill for companies table.

For each unique current_company_url across a filtered lead set, calls
/api/v1/company/profile with the numeric company_id from the URL, then
UPSERTs name/website/industry/employee_count/employee_range into the
`companies` table.

Cost: ~1 Saleleads credit per UNIQUE company (~50% dedupe across leads).

CLI:
  python3 fill_company_size.py --client Acme-AI \\
      --where-tag engager:acme-ai-potential-thought-leaders-2026-06-06 \\
      --where-persona "demand gen practitioner,demand gen service provider"

  python3 fill_company_size.py --client Acme-AI \\
      --where-tag engager:acme-ai-potential-thought-leaders-2026-06-06 \\
      --refresh-days 30
"""
from __future__ import annoacmens

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, load_client_env
from engagers_research import saleleads_get


_NUMERIC_ID_RE = re.compile(r"/company/(\d+)/?")


def extract_company_id(url: str) -> str | None:
    if not url:
        return None
    m = _NUMERIC_ID_RE.search(url)
    return m.group(1) if m else None


def fetch_company_profile(company_id: str) -> dict | None:
    res = saleleads_get("/api/v1/company/profile", {"company_id": company_id})
    if not res or not res.get("success"):
        return None
    return res.get("data") or None


def select_target_urls(cur, *, where_tag, where_persona, refresh_days):
    sql = """
      SELECT DISTINCT l.current_company_url
      FROM leads l
    """
    where: list[str] = ["l.current_company_url IS NOT NULL", "l.current_company_url <> ''"]
    params: list = []

    if where_tag:
        sql += " JOIN lead_tags t ON t.lead_id = l.id "
        where.append("t.tag = %s"); params.append(where_tag)

    if where_persona:
        sql += " JOIN lead_qualifications lq ON lq.lead_id = l.id "
        where.append("""(
          (lq.qualifier_name='demand_gen_headline_persona_classifier' AND lq.persona = ANY(%s))
          OR
          (lq.qualifier_name='demand_gen_persona_classifier' AND
            CASE lq.full_result->>'type'
              WHEN 'practitioner' THEN 'demand gen practitioner'
              WHEN 'service_provider' THEN 'demand gen service provider'
            END = ANY(%s))
        )""")
        params.extend([where_persona, where_persona])

    sql += " WHERE " + " AND ".join(where)
    cur.execute(sql, params)
    urls = [r[0] for r in cur.fetchall()]

    # If --refresh-days, skip companies whose size_fetched_at is fresh enough
    if refresh_days is not None and urls:
        ids = [(u, extract_company_id(u)) for u in urls]
        sl_ids = [cid for _, cid in ids if cid]
        if sl_ids:
            cur.execute("""
              SELECT saleleads_id FROM companies
              WHERE saleleads_id = ANY(%s)
                AND size_fetched_at IS NOT NULL
                AND size_fetched_at > now() - (%s || ' days')::interval
            """, (sl_ids, refresh_days))
            fresh = {r[0] for r in cur.fetchall()}
            urls = [u for u, cid in ids if not cid or cid not in fresh]
    return urls


def upsert_company(cur, *, sl_id, name, linkedin_url, website, industry, ec, er):
    # Derive slug from URL
    slug = ""
    if linkedin_url:
        m = re.search(r"linkedin\.com/company/([^/?#]+)", linkedin_url)
        if m:
            slug = m.group(1)
    cur.execute("""
      INSERT INTO companies (linkedin_slug, name, linkedin_url, saleleads_id, website,
                             industry, employee_count, employee_range, size_fetched_at)
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
      ON CONFLICT (linkedin_slug) DO UPDATE SET
        name             = COALESCE(NULLIF(companies.name, ''),           EXCLUDED.name),
        linkedin_url     = COALESCE(NULLIF(companies.linkedin_url, ''),   EXCLUDED.linkedin_url),
        saleleads_id     = COALESCE(NULLIF(companies.saleleads_id, ''),   EXCLUDED.saleleads_id),
        website          = COALESCE(NULLIF(companies.website, ''),        EXCLUDED.website),
        industry         = COALESCE(NULLIF(companies.industry, ''),       EXCLUDED.industry),
        employee_count   = COALESCE(EXCLUDED.employee_count, companies.employee_count),
        employee_range   = COALESCE(NULLIF(EXCLUDED.employee_range, ''),  companies.employee_range),
        size_fetched_at  = now()
    """, (slug or sl_id, name, linkedin_url, sl_id, website, industry, ec, er))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--where-tag")
    ap.add_argument("--where-persona", help="Comma-separated personas.")
    ap.add_argument("--refresh-days", type=int, default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    load_client_env(args.client)
    personas = ([p.strip() for p in args.where_persona.split(",") if p.strip()]
                if args.where_persona else None)

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            urls = select_target_urls(
                cur, where_tag=args.where_tag,
                where_persona=personas, refresh_days=args.refresh_days)
        if args.limit:
            urls = urls[:args.limit]

        skipped_bad_url = 0
        candidates = []
        for u in urls:
            cid = extract_company_id(u)
            if not cid:
                skipped_bad_url += 1
                continue
            candidates.append((u, cid))

        if not candidates:
            print(f"Nothing to fetch (skipped non-numeric URLs: {skipped_bad_url}).")
            return 0
        print(f"Companies to fetch: {len(candidates)} (skipped non-numeric: {skipped_bad_url})")

        from engagers_research import (
            SaleleadsCreditExhausted as _SaleleadsCreditExhausted,
            saleleads_cost_snapshot,
        )

        updated = no_data = errors = 0
        for i, (url, cid) in enumerate(candidates, 1):
            try:
                d = fetch_company_profile(cid)
                if not d:
                    no_data += 1
                    continue
                ec = d.get("staff_count") or d.get("employee_count")
                er = d.get("staff_count_range") or d.get("employee_range") or ""
                inds = d.get("industries") or []
                industry = ", ".join(inds[:2]) if inds else (d.get("industry") or "")
                website = d.get("website_url") or d.get("website") or ""
                name = d.get("name") or ""
                # Prefer canonical linkedin_url from API; fall back to source URL
                api_url = d.get("url") or d.get("linkedin_url") or url

                with conn.cursor() as cur:
                    upsert_company(cur, sl_id=cid, name=name, linkedin_url=api_url,
                                   website=website, industry=industry, ec=ec, er=er)
                conn.commit()
                updated += 1
                if i % 25 == 0 or updated <= 5:
                    size = (f"{ec:,}" if ec else er or "?")
                    print(f"[{i}/{len(candidates)}] ✓ {name[:35]}: {size}  ({industry[:30]})")
            except _SaleleadsCreditExhausted as e:
                print(f"\n✖ ABORTING: {e}", file=sys.stderr)
                print(f"  Processed {i-1}/{len(candidates)}; resume by re-running.",
                      file=sys.stderr)
                return 2
            except Exception as e:
                errors += 1
                print(f"[{i}/{len(candidates)}] ✗ company_id={cid}: {e}")

        print(f"\nSummary: updated={updated}  no-data={no_data}  errors={errors}  skipped(bad-url)={skipped_bad_url}")
        try:
            s = saleleads_cost_snapshot()
            print(f"  saleleads: calls={s['calls_total']}  success={s['calls_success']}  "
                  f"charged_denials={s['calls_charged_denial']}  "
                  f"cost_units_charged={s['cost_charged']}  cost_units_free={s['cost_free']}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
