#!/usr/bin/env python3
"""Import each active LinkedIn operator's 1st-degree connections from the
client's own Unipile workspace into the client's MarketBase.

EXCEPTION to the standing "no Unipile for scraping" rule: pulling an
operator's OWN 1st-degree connection list does not scrape LinkedIn — the
connections are already synced inside Unipile from when the LinkedIn account
was connected. Calls to `/api/v1/users/relations` read Unipile's internal
copy, not LinkedIn. (Pulling a profile detail or sending an unsolicited
message would, by contrast, hit LinkedIn and burn the account.)

For each active LinkedIn operator in `outbound_operators`:
  • Paginate `/api/v1/users/relations?account_id=<id>` via cursor.
  • UPSERT each connection into `leads` (keyed on canonical linkedin_url).
  • Append a `lead_sources` row with source_type='founder_network_export',
    source_label='<Operator name> 1st-degree connections (Unipile sync)',
    raw_data=<the Unipile relation record>.

Per-batch flushing every K=50 rows (CONVENTIONS durability rule).
Idempotent — re-runs UPSERT leads and append new lead_sources rows; for
re-syncs we keep multiple lead_sources rows so we can see when a connection
was first observed.
"""
from __future__ import annoacmens

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, normalize_linkedin_url, load_client_env, resolve_canonical_url  # noqa: E402

SOURCE_TYPE = "founder_network_export"
PAGE_SIZE = 200
FLUSH_EVERY = 50


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing env var: {name} (load ~/.env.<Client> first)")
    return v


def fetch_active_operators(client: str) -> list[dict]:
    """Return active LinkedIn operators for the client's MarketBase."""
    with connect(client) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT unipile_account_id, display_name
            FROM outbound_operators
            WHERE is_active = TRUE AND channel = 'linkedin'
              AND unipile_account_id IS NOT NULL
            ORDER BY display_name
        """)
        return [{"id": r[0], "name": r[1]} for r in cur.fetchall()]


def unipile_paginate(base: str, key: str, account_id: str):
    """Yield each relation record by paginating Unipile's /users/relations."""
    cursor = None
    fetched = 0
    while True:
        params = {"account_id": account_id, "limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        url = f"{base}/api/v1/users/relations?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-API-KEY": key})
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.loads(r.read())
        items = payload.get("items") or []
        fetched += len(items)
        for item in items:
            yield item
        cursor = payload.get("cursor")
        if not cursor or not items:
            return
        time.sleep(0.2)  # gentle pacing


def _connection_linkedin_url(rel: dict) -> str | None:
    """Best-effort canonical linkedin_url from a Unipile relation record."""
    pub = (rel.get("public_identifier") or "").strip()
    if pub:
        return normalize_linkedin_url(f"https://www.linkedin.com/in/{pub}/")
    full = (rel.get("public_profile_url") or "").strip()
    if full:
        return normalize_linkedin_url(full)
    return None


def import_for_operator(client: str, op: dict, base: str, key: str,
                        limit: int | None) -> dict:
    print(f"\n--- {op['name']} ({op['id']}) ---", flush=True)
    source_label = f"{op['name']} 1st-degree connections (Unipile sync)"
    inserted_leads = appended_sources = skipped_no_url = 0

    with connect(client) as conn, conn.cursor() as cur:
        since_flush = 0
        for i, rel in enumerate(unipile_paginate(base, key, op["id"])):
            if limit and i >= limit:
                break
            li_url = _connection_linkedin_url(rel)
            if not li_url:
                skipped_no_url += 1
                continue
            first = (rel.get("first_name") or "").strip()
            last = (rel.get("last_name") or "").strip()
            name = (f"{first} {last}").strip()
            headline = (rel.get("headline") or "").strip() or None
            urn = rel.get("member_urn")
            public_id = rel.get("public_identifier")
            li_url = resolve_canonical_url(cur, normalize_linkedin_url(li_url),
                                           urn_hint=urn)
            cur.execute("""
                INSERT INTO leads (linkedin_url, linkedin_urn, public_id, name,
                                   headline, last_enriched_at)
                VALUES (%(url)s, %(urn)s, %(pid)s, %(name)s, %(hl)s, now())
                ON CONFLICT (linkedin_url) DO UPDATE SET
                    linkedin_urn = COALESCE(EXCLUDED.linkedin_urn, leads.linkedin_urn),
                    public_id    = COALESCE(EXCLUDED.public_id,    leads.public_id),
                    name         = COALESCE(leads.name,            EXCLUDED.name),
                    headline     = COALESCE(leads.headline,        EXCLUDED.headline),
                    updated_at   = now()
                RETURNING id, (xmax = 0) AS inserted
            """, {"url": li_url, "urn": urn, "pid": public_id,
                  "name": name or None, "hl": headline})
            lead_id, was_inserted = cur.fetchone()
            if was_inserted:
                inserted_leads += 1
            cur.execute("""
                INSERT INTO lead_sources (lead_id, source_type, source_label,
                                          source_date, raw_data)
                VALUES (%s, %s, %s, %s, %s::jsonb)
            """, (lead_id, SOURCE_TYPE, source_label,
                  date.today(), json.dumps(rel)))
            appended_sources += 1
            since_flush += 1
            if since_flush >= FLUSH_EVERY:
                conn.commit()
                since_flush = 0
            if appended_sources % 200 == 0:
                print(f"    ... {appended_sources} relations imported", flush=True)
        conn.commit()

    print(f"  ✓ {op['name']}: {appended_sources} relations imported "
          f"(new leads: {inserted_leads}, skipped no-url: {skipped_no_url})",
          flush=True)
    return {"operator": op["name"], "imported": appended_sources,
            "new_leads": inserted_leads, "skipped_no_url": skipped_no_url}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True,
                    help="Client name; loads ~/.env.<Client>.")
    ap.add_argument("--operator-name",
                    help="Limit to one operator (matches display_name substring).")
    ap.add_argument("--limit", type=int,
                    help="Cap connections per operator (for testing).")
    args = ap.parse_args()

    load_client_env(args.client)
    base = "https://" + _env("UNIPILE_BASE_SERVER")
    key = _env("UNIPILE_ACCESS_TOKEN")

    ops = fetch_active_operators(args.client)
    if args.operator_name:
        ops = [o for o in ops if args.operator_name.lower() in o["name"].lower()]
    if not ops:
        sys.exit("No matching active LinkedIn operators in outbound_operators.")
    print(f"Importing connections for {len(ops)} operator(s) "
          f"in {args.client}: {[o['name'] for o in ops]}", flush=True)

    results = []
    t0 = time.time()
    for op in ops:
        try:
            results.append(import_for_operator(args.client, op, base, key, args.limit))
        except Exception as e:
            print(f"  ✗ {op['name']}: {type(e).__name__}: {e}", flush=True)
            results.append({"operator": op["name"], "error": str(e)})

    print(f"\n=== DONE in {time.time()-t0:.0f}s ===")
    for r in results:
        if "error" in r:
            print(f"  ✗ {r['operator']:<25} ERROR: {r['error'][:80]}")
        else:
            print(f"  ✓ {r['operator']:<25} imported={r['imported']:<6} "
                  f"new_leads={r['new_leads']:<6} skipped={r['skipped_no_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
