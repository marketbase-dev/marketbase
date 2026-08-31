#!/usr/bin/env python3
"""fill_current_position — backfill leads.current_company / current_title /
current_company_url via Saleleads /user/experience.

WHY THIS EXISTS: fill_basic_profile.py uses Saleleads /user/profile, which only
returns headline/name/location/public_id — it does NOT return structured work
history, so it can't populate current_company. The CURRENT POSITION lives behind
/user/experience. This tool reuses qualify-acme-target's `_enrich_via_saleleads`
(the same /user/experience path) and writes the result back into the leads table.

Skips leads that already have a current_company unless --refresh.

Usage:
  python3 fill_current_position.py --client Acme --where-tag copperhelm_engager_persona_queued
  python3 fill_current_position.py --client Acme --lead-file leads.csv --refresh
"""
from __future__ import annotations
import argparse, sys, csv as _csv
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path.home() / ".claude/tools/MarketBase"))
sys.path.insert(0, str(Path.home() / ".claude/tools/acme-qualify"))
from lib import connect                       # type: ignore  # noqa: E402
from qualify import enrich_person             # type: ignore  # noqa: E402


def _targets(cur, where_tag, lead_file, refresh):
    base = "SELECT id, linkedin_url, name, current_company FROM leads"
    if lead_file:
        urls = []
        with open(lead_file, newline="") as f:
            rdr = _csv.DictReader(f)
            col = next((c for c in rdr.fieldnames
                        if c.lower() in ("linkedin_url", "linkedin url", "profile_url", "url")), None)
            if not col:
                sys.exit(f"no LinkedIn URL column in {lead_file}; cols={rdr.fieldnames}")
            for r in rdr:
                u = (r.get(col) or "").strip()
                if u:
                    urls.append(u)
        cur.execute(base + " WHERE linkedin_url = ANY(%s)", (urls,))
    elif where_tag:
        cur.execute(base + " WHERE id IN (SELECT lead_id FROM lead_tags WHERE tag=%s)", (where_tag,))
    else:
        sys.exit("provide --where-tag or --lead-file")
    rows = [{"id": r[0], "linkedin_url": r[1], "name": r[2], "current_company": r[3]}
            for r in cur.fetchall()]
    if not refresh:
        rows = [r for r in rows if not (r["current_company"] or "").strip()]
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--where-tag")
    g.add_argument("--lead-file")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-fetch even if current_company is already populated.")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    with connect(args.client) as conn, conn.cursor() as cur:
        targets = _targets(cur, args.where_tag, args.lead_file, args.refresh)
    if args.limit:
        targets = targets[: args.limit]
    print(f"Targets needing current-position backfill: {len(targets)}", flush=True)

    def work(t):
        try:
            e = enrich_person(t["linkedin_url"])
        except Exception as ex:
            return (t, None, f"{type(ex).__name__}: {ex}")
        if "_error" in e:
            return (t, None, e["_error"])
        return (t, e, None)

    updated = skipped = errors = done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool, \
         connect(args.client) as conn:
        futs = {pool.submit(work, t): t for t in targets}
        for fut in as_completed(futs):
            t, e, err = fut.result()
            done += 1
            if err or not e:
                errors += 1
                print(f"[{done}/{len(targets)}] ! {t['name'] or t['linkedin_url']}: {err}", flush=True)
                continue
            co = (e.get("current_company") or "").strip() or None
            title = (e.get("job_title") or "").strip() or None
            curl = (e.get("company_linkedin_url") or "").strip() or None
            if not (co or title):
                skipped += 1
                continue
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE leads SET
                      current_company     = COALESCE(NULLIF(current_company, ''),     %s),
                      current_title       = COALESCE(NULLIF(current_title, ''),       %s),
                      current_company_url = COALESCE(NULLIF(current_company_url, ''), %s),
                      updated_at = now()
                    WHERE id = %s
                """ if not args.refresh else """
                    UPDATE leads SET
                      current_company     = COALESCE(%s, current_company),
                      current_title       = COALESCE(%s, current_title),
                      current_company_url = COALESCE(%s, current_company_url),
                      updated_at = now()
                    WHERE id = %s
                """, (co, title, curl, t["id"]))
            conn.commit()
            updated += 1
            if done % 50 == 0 or done == len(targets):
                print(f"[{done}/{len(targets)}] updated={updated} skipped={skipped} errors={errors}", flush=True)

    print(f"DONE: updated={updated} skipped(no-company)={skipped} errors={errors}", flush=True)


if __name__ == "__main__":
    main()
