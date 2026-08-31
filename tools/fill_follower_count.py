#!/usr/bin/env python3
"""fill_follower_count — Saleleads /search/people backfill for follower count.

For each target lead, calls /api/v1/search/people with the lead's name,
disambiguates results by URN (preferred) or public_identifier, then writes
`follower_count_display` to `leads.follower_count`.

Cost: ~1 Saleleads credit per lead.

Selection criteria are filters that compose with AND:
  --where-tag TAG                    only leads with this tag
  --where-persona "p1,p2,..."        only leads whose qualifier persona is in this list
                                     (checks both cheap & heavy classifiers)
  --where-blank-follower             only leads where follower_count IS NULL
  --refresh-days N                   re-fetch only if data is older than N days
  --limit N                          cap per run

CLI:
  python3 fill_follower_count.py --client Acme-AI \\
      --where-tag engager:acme-ai-potential-thought-leaders-2026-06-06 \\
      --where-persona "demand gen practitioner,demand gen service provider"
"""
from __future__ import annoacmens

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, load_client_env
from engagers_research import saleleads_get


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_follower_count(name: str, urn: str | None, public_id: str | None) -> tuple[bool, int | None]:
    """Search by name; match the right person by URN or public_identifier.

    Returns (matched, follower_count). `matched=True` means we found the
    person in the search results; `follower_count` may still be None if
    Saleleads's response omits it for them.
    """
    if not name:
        return (False, None)
    res = saleleads_get("/api/v1/search/people", {"name": name, "page": 1})
    if not res or not res.get("success"):
        return (False, None)
    candidates = res.get("data") or []
    if not isinstance(candidates, list) or not candidates:
        return (False, None)

    # Match by URN first (most precise)
    if urn:
        for p in candidates:
            if (p.get("urn") or "") == urn:
                return (True, p.get("follower_count_display"))

    # Fall back to public_identifier (skip if it looks like a URN — bad data)
    if public_id and not public_id.startswith(("ACo", "ACw")):
        pid_norm = public_id.strip("/").lower()
        for p in candidates:
            if (p.get("public_identifier") or "").strip("/").lower() == pid_norm:
                return (True, p.get("follower_count_display"))

    # Last resort: name-exact match if only one result
    name_norm = _norm(name)
    exact = [p for p in candidates if _norm(p.get("full_name", "")) == name_norm]
    if len(exact) == 1:
        return (True, exact[0].get("follower_count_display"))

    return (False, None)


def select_targets(cur, *, where_tag: str | None, where_persona: list[str] | None,
                   where_blank: bool, refresh_days: int | None) -> list[dict]:
    sql = """
      SELECT DISTINCT l.id, l.name, l.linkedin_url, l.linkedin_urn, l.public_id,
             l.follower_count, l.follower_count_updated_at
      FROM leads l
    """
    where: list[str] = []
    params: list = []

    if where_tag:
        sql += " JOIN lead_tags t ON t.lead_id = l.id "
        where.append("t.tag = %s")
        params.append(where_tag)

    if where_persona:
        sql += """
          JOIN lead_qualifications lq ON lq.lead_id = l.id
        """
        # Match either cheap classifier's persona, or heavy classifier's normalized type
        where.append("""(
          (lq.qualifier_name = 'demand_gen_headline_persona_classifier' AND lq.persona = ANY(%s))
          OR
          (lq.qualifier_name = 'demand_gen_persona_classifier' AND
           CASE lq.full_result->>'type'
             WHEN 'practitioner'     THEN 'demand gen practitioner'
             WHEN 'service_provider' THEN 'demand gen service provider'
           END = ANY(%s))
        )""")
        params.extend([where_persona, where_persona])

    if where_blank:
        where.append("l.follower_count IS NULL")

    if refresh_days is not None:
        where.append("(l.follower_count_updated_at IS NULL "
                     "OR l.follower_count_updated_at < now() - (%s || ' days')::interval)")
        params.append(refresh_days)

    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY l.id"

    cur.execute(sql, params)
    cols = ["id", "name", "linkedin_url", "linkedin_urn", "public_id",
            "follower_count", "follower_count_updated_at"]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--where-tag")
    ap.add_argument("--where-persona", help="Comma-separated personas to match.")
    ap.add_argument("--where-blank-follower", action="store_true")
    ap.add_argument("--refresh-days", type=int, default=None,
                    help="Skip leads whose follower_count was updated within this many days.")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    load_client_env(args.client)

    personas = ([p.strip() for p in args.where_persona.split(",") if p.strip()]
                if args.where_persona else None)

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            targets = select_targets(
                cur,
                where_tag=args.where_tag,
                where_persona=personas,
                where_blank=args.where_blank_follower,
                refresh_days=args.refresh_days,
            )
        if args.limit:
            targets = targets[:args.limit]
        if not targets:
            print("No leads match the filter.")
            return 0
        print(f"Targets: {len(targets):,} lead(s)")

        from engagers_research import (
            SaleleadsCreditExhausted as _SaleleadsCreditExhausted,
            saleleads_cost_snapshot,
        )

        updated = matched_no_count = not_found = errors = 0
        for i, t in enumerate(targets, 1):
            try:
                matched, fc = find_follower_count(t["name"], t["linkedin_urn"], t["public_id"])
                if not matched:
                    not_found += 1
                    if i % 50 == 0 or not_found <= 3:
                        print(f"[{i}/{len(targets)}] ⊝ not found: {t['name']}")
                    continue
                # Found the person — record the attempt either way.
                with conn.cursor() as cur:
                    cur.execute("""
                      UPDATE leads
                      SET follower_count = COALESCE(%s, follower_count),
                          follower_count_updated_at = now()
                      WHERE id = %s
                    """, (fc, t["id"]))
                conn.commit()
                if fc is None:
                    matched_no_count += 1
                    if i % 25 == 0 or matched_no_count <= 5:
                        print(f"[{i}/{len(targets)}] ◐ {t['name']}: matched but no follower count")
                else:
                    updated += 1
                    if i % 25 == 0 or updated <= 5:
                        print(f"[{i}/{len(targets)}] ✓ {t['name']}: {fc:,} followers")
            except _SaleleadsCreditExhausted as e:
                print(f"\n✖ ABORTING: {e}", file=sys.stderr)
                print(f"  Processed {i-1}/{len(targets)}; resume by re-running.",
                      file=sys.stderr)
                return 2
            except Exception as e:
                errors += 1
                print(f"[{i}/{len(targets)}] ✗ {t['name']}: {e}")

        print(f"\nSummary: updated={updated}  matched-no-count={matched_no_count}  not-found={not_found}  errors={errors}")
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
