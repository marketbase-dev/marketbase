#!/usr/bin/env python3
"""marketbase-repair-lead-names — replace a formatted-None lead name ("None None")
with the person's real name, or with NULL when no source has one.

Why (2026-11-05): vendor normalisers built names as
f"{p.get('first_name','')} {p.get('last_name','')}", and a key present with a
null value formatted as "None". 97 leads across six instances were stored as
"None None". The writers are fixed (~/.claude/tools/person_name.py) and 084 makes
the database refuse such a name; this repairs the rows already there, so 085 can
validate the guard.

Sources, in order, first real name wins:
  1. a cached vendor response in enrichment_calls for the lead's profile URL
     (any api: the name the vendor returned, never a guess);
  2. Comp Intel's persons row for the same URL, when SUPABASE_COMPETITOR_INTEL_POOLER_URL
     is set (read-only);
  3. with --fetch: a saleleads /api/v1/user/profile lookup, cached in
     enrichment_calls through api_cache before it is used (paid; one per lead).
No source: the name becomes NULL. A NULL name is honest; "None None" is not.

Every change is written by id with the old value asserted (WHERE id AND name),
rowcount 1, and leaves an assertions row (namespace lead_field, key name) saying
what it replaced and where the new value came from.

DEFAULT IS A DRY RUN. --execute --expect N commits only if N rows change.

Usage:
  python3 repair_lead_names.py --client X [--fetch]
  python3 repair_lead_names.py --client X [--fetch] --execute --expect N
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

BAD = r"(^|\s)None(\s|$)"
NAME_IN_RESPONSE = """coalesce(response->'person'->>'full_name', response->'data'->>'full_name',
                               response->>'full_name', response->'data'->>'name',
                               nullif(btrim(concat_ws(' ', response->'data'->>'first_name',
                                                      response->'data'->>'last_name')), ''))"""


def real(name: str | None) -> str | None:
    import re
    n = (name or "").strip()
    return n if n and not re.search(BAD, n) else None


def handle(url: str | None) -> str:
    return (url or "").rstrip("/").split("/")[-1]


def cached_name(cur, lid: str, url: str) -> tuple[str | None, str | None]:
    cur.execute(f"""SELECT id::text, api, endpoint, {NAME_IN_RESPONSE}
                      FROM enrichment_calls
                     WHERE success AND (lead_id = %s OR params::text ILIKE %s)
                     ORDER BY fetched_at DESC""", (lid, f"%/in/{handle(url)}%"))
    for cid, api, ep, nm in cur.fetchall():
        if real(nm):
            return real(nm), f"enrichment_calls {cid} ({api} {ep})"
    return None, None


def comp_intel_names(urls: list[str]) -> dict[str, str]:
    dsn = os.environ.get("SUPABASE_COMPETITOR_INTEL_POOLER_URL")
    if not dsn or not urls:
        return {}
    import psycopg
    with psycopg.connect(dsn) as ci:
        cur = ci.cursor()
        cur.execute("SET default_transaction_read_only = on")
        cur.execute("""SELECT lower(rtrim(linkedin_url, '/')), name FROM persons
                        WHERE lower(rtrim(linkedin_url, '/')) = ANY(%s) AND merged_into IS NULL""",
                    ([u.lower().rstrip("/") for u in urls],))
        return {u: n for u, n in cur.fetchall() if real(n)}


def fetched_name(conn, url: str) -> tuple[str | None, str | None]:
    import saleleads
    resp = saleleads.user_profile(handle(url), cache_conn=conn)
    cur = conn.cursor()
    cur.execute("""SELECT id::text FROM enrichment_calls WHERE api = 'saleleads'
                     AND endpoint = '/api/v1/user/profile' AND params->>'username' = %s
                   ORDER BY fetched_at DESC LIMIT 1""", (handle(url),))
    row = cur.fetchone()
    d = (resp or {}).get("data") if isinstance(resp, dict) else None
    d = d if isinstance(d, dict) else (resp if isinstance(resp, dict) else {})
    nm = d.get("full_name") or d.get("name") or " ".join(
        x for x in (d.get("first_name"), d.get("last_name")) if x)
    return real(nm), (f"enrichment_calls {row[0]} (saleleads /api/v1/user/profile)" if row else
                      "saleleads /api/v1/user/profile")


def main() -> int:
    ap = argparse.ArgumentParser(description="Repair formatted-None lead names.")
    ap.add_argument("--client", required=True)
    ap.add_argument("--fetch", action="store_true", help="paid: look up names no free source has")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--expect", type=int)
    args = ap.parse_args()
    if args.execute and args.expect is None:
        sys.exit("--execute needs --expect N from the dry run")

    conn = lib.connect(args.client)
    cur = conn.cursor()
    cur.execute("SELECT id::text, name, linkedin_url FROM leads WHERE name ~ %s ORDER BY id", (BAD,))
    bad = cur.fetchall()
    ci = comp_intel_names([u for _, _, u in bad if u])

    plan, by_source = [], collections.Counter()
    for lid, old, url in bad:
        new, src = cached_name(cur, lid, url)
        if not new and url and url.lower().rstrip("/") in ci:
            new, src = ci[url.lower().rstrip("/")], "Comp Intel persons (same profile URL)"
        if not new and args.fetch and url:
            new, src = fetched_name(conn, url)
        by_source[src.split(" (")[0].split(" ")[0] if src and new else "none: set to NULL"] += 1
        plan.append((lid, old, new, src if new else "no source has a name"))

    print(f"{args.client}: {len(bad)} formatted-None name(s); " +
          ", ".join(f"{k} {v}" for k, v in by_source.most_common()))
    for lid, old, new, src in plan[:8]:
        print(f"  {lid}  {old!r} -> {new!r}  ({src})")
    if not args.execute:
        conn.rollback()
        print(f"Dry run: nothing written. To repair: --execute --expect {len(plan)}" +
              ("" if args.fetch else "  (add --fetch to look up the rest, paid)"))
        return 0
    if args.expect != len(plan):
        conn.rollback()
        sys.exit(f"Refusing: {len(plan)} row(s) now, not the approved {args.expect}.")
    for lid, old, new, src in plan:
        cur.execute("UPDATE leads SET name = %s, updated_at = now() WHERE id = %s AND name = %s",
                    (new, lid, old))
        if cur.rowcount != 1:
            conn.rollback()
            sys.exit(f"{lid}: expected 1 row, got {cur.rowcount}; rolled back")
        cur.execute("""INSERT INTO assertions (subject_lead_id, namespace, key, value, notes, asserted_by)
                       VALUES (%s, 'lead_field', 'name', %s, %s, 'repair_lead_names')""",
                    (lid, new or "", f"replaced the formatted-None name {old!r}; source: {src}"))
    conn.commit()
    print(f"REPAIRED {len(plan)} name(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
