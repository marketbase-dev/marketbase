#!/usr/bin/env python3
"""marketbase-dedupe-leads — merge duplicate person rows onto one canonical lead.

Identity bug being fixed: `leads` was keyed on `linkedin_url` (the URL string),
so the same real person ingested under a vanity URL (/in/ryanwinstanley) AND a
URN URL (/in/ACoAA…) became two rows. They share the same LinkedIn member URN
(`leads.member_urn`, added in migration 027), which IS the real identity.

This tool groups leads by member_urn, picks the canonical row per group, moves
every child record (campaign_members, conversations, tags, qualifications,
sources, signals, meetings, actions, enrichment_calls, post_engagements) onto
it — deduping on each child's own unique key — COALESCE-fills the canonical's
empty identity columns from the duplicates, then deletes the duplicates. Both
reps' conversations consolidate onto the one person (MarketBase already models
many-reps-per-person via lead_conversations(operator_id, lead_id, channel)).

DEFAULT IS DRY-RUN: it runs the full merge in a transaction, prints exact
rowcounts, then ROLLS BACK. Pass --execute to commit. After a committed run
with zero duplicate groups remaining, it creates the partial-unique index
uq_leads_member_urn (WHERE member_urn IS NOT NULL) so duplicates can't re-form.

Usage:
  python3 dedupe_leads.py --client Acme                # dry-run report
  python3 dedupe_leads.py --client Acme --execute      # commit the merge
  python3 dedupe_leads.py --client Acme --limit 20     # first 20 groups
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect


# Every table with a lead_id FK → leads(id), mapped to the columns (besides
# lead_id) of its lead-scoped unique key. A row from a duplicate can move to the
# canonical only if the canonical has no row with the same values in those
# columns; collisions are deleted (the canonical already has the equivalent).
# Empty list = no lead-scoped unique key → every row moves unconditionally.
# Verified against information_schema / pg_indexes for the Acme DB.
CHILD_SPEC = {
    "campaign_members":    ["campaign_id"],
    "enrichment_calls":    [],                          # FK is SET NULL, pkey only
    "lead_actions":        [],
    "lead_conversations":  ["operator_id", "channel"],  # uq_lconv_operator_lead_channel
    "lead_meetings":       [],
    "lead_qualifications": [],
    "lead_signals":        ["enricher_name", "enricher_version"],
    "lead_sources":        [],
    "lead_tags":           ["tag"],                     # uq_lead_tags_lead_tag
    "post_engagements":    [],
}

# leads scalar columns we COALESCE-fill onto the canonical from duplicates so a
# name/title/company that only the duplicate had is not lost. (Identity columns
# only — linkedin_url stays the canonical's; member_urn is identical by group.)
FILL_COLS = [
    "linkedin_urn", "public_id", "name", "headline", "current_title",
    "current_company", "current_company_url", "city", "country", "bio",
    "last_enriched_at",
]

UNIQUE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_member_urn "
    "ON leads(member_urn) WHERE member_urn IS NOT NULL"
)


def assert_child_set_current(cur) -> None:
    """Fail loudly if a table references leads(id) that CHILD_SPEC doesn't cover
    — otherwise a merge would silently orphan or lose its rows."""
    cur.execute("""
        SELECT DISTINCT tc.table_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type='FOREIGN KEY'
          AND ccu.table_name='leads' AND ccu.column_name='id'
    """)
    actual = {r[0] for r in cur.fetchall()}
    missing = actual - set(CHILD_SPEC)
    extra = set(CHILD_SPEC) - actual
    if missing:
        sys.exit(f"ABORT: tables reference leads(id) but are not in CHILD_SPEC: "
                 f"{sorted(missing)}. Add them before merging.")
    if extra:
        print(f"  note: CHILD_SPEC lists tables with no current FK (harmless): "
              f"{sorted(extra)}")


def pick_canonical(rows: list[dict]) -> dict:
    """Choose the survivor. Prefer a URN-form linkedin_url (/in/AC…, the stable
    form), then the most-enriched / most-recently-updated / oldest row."""
    def key(r):
        urn_form = 1 if "/in/AC" in (r["linkedin_url"] or "") else 0
        return (
            urn_form,
            r["last_enriched_at"] or r["created_at"],   # most enriched
            r["updated_at"],                             # then freshest
        )
    # Highest key wins; created_at asc as final stabilizer handled by sort.
    return sorted(rows, key=key, reverse=True)[0]


def merge_group(cur, canon_id, dup_ids: list, stats: dict) -> None:
    for table, dedupe_cols in CHILD_SPEC.items():
        for dup in dup_ids:
            if dedupe_cols:
                cond = " AND ".join(
                    f"x.{c} IS NOT DISTINCT FROM t.{c}" for c in dedupe_cols
                )
                cur.execute(f"""
                    UPDATE {table} t SET lead_id = %s
                     WHERE t.lead_id = %s
                       AND NOT EXISTS (SELECT 1 FROM {table} x
                                        WHERE x.lead_id = %s AND {cond})
                """, (canon_id, dup, canon_id))
                stats[f"{table}:moved"] += cur.rowcount
                cur.execute(f"DELETE FROM {table} WHERE lead_id = %s", (dup,))
                stats[f"{table}:collisions_deleted"] += cur.rowcount
            else:
                cur.execute(f"UPDATE {table} SET lead_id = %s WHERE lead_id = %s",
                            (canon_id, dup))
                stats[f"{table}:moved"] += cur.rowcount

    # COALESCE-fill the canonical's empty identity columns from the duplicates.
    set_clause = ", ".join(f"{c} = COALESCE(c.{c}, d.{c})" for c in FILL_COLS)
    cur.execute(f"""
        UPDATE leads c SET {set_clause}
        FROM (SELECT {", ".join(FILL_COLS)} FROM leads
               WHERE id = ANY(%s) ORDER BY updated_at DESC) d
        WHERE c.id = %s
    """, (dup_ids, canon_id))

    # The deleted twin's URL string disappears with it. If the survivor has no
    # vanity handle yet, inherit the twin's so a later vanity re-upload still
    # resolves to this person (lib.resolve_canonical_url → public_id fallback).
    cur.execute("""
        UPDATE leads c SET public_id = d.slug
        FROM (SELECT substring(linkedin_url from '/in/([^/?#]+)') AS slug
              FROM leads
              WHERE id = ANY(%s) AND linkedin_url !~ '/in/AC'
                AND substring(linkedin_url from '/in/([^/?#]+)') IS NOT NULL
              ORDER BY updated_at DESC LIMIT 1) d
        WHERE c.id = %s AND c.public_id IS NULL
    """, (dup_ids, canon_id))

    cur.execute("DELETE FROM leads WHERE id = ANY(%s)", (dup_ids,))
    stats["leads:deleted"] += cur.rowcount
    stats["groups"] += 1


def main() -> int:
    p = argparse.ArgumentParser(description="Merge duplicate person rows by member_urn.")
    p.add_argument("--client", required=True)
    p.add_argument("--execute", action="store_true",
                   help="Commit the merge (default: dry-run + rollback).")
    p.add_argument("--limit", type=int, help="Process only the first N groups.")
    args = p.parse_args()

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"[{mode}] dedupe leads by member_urn for {args.client}\n")

    conn = connect(args.client)
    with conn.cursor() as cur:
        assert_child_set_current(cur)

        cur.execute("""
            SELECT member_urn, array_agg(id ORDER BY created_at) AS ids
            FROM leads
            WHERE member_urn IS NOT NULL
            GROUP BY member_urn
            HAVING count(*) > 1
            ORDER BY member_urn
        """)
        groups = cur.fetchall()
        if args.limit:
            groups = groups[:args.limit]
        print(f"duplicate member_urn groups: {len(groups)}"
              + (f" (limited to {args.limit})" if args.limit else ""))
        if not groups:
            print("nothing to merge.")
            if args.execute:
                cur.execute(UNIQUE_INDEX_SQL); conn.commit()
                print(f"ensured unique index: {UNIQUE_INDEX_SQL}")
            return 0

        stats = {"groups": 0, "leads:deleted": 0}
        for t in CHILD_SPEC:
            stats[f"{t}:moved"] = 0
            if CHILD_SPEC[t]:
                stats[f"{t}:collisions_deleted"] = 0

        for member_urn, ids in groups:
            # Re-fetch full rows for canonical selection.
            cur.execute("""
                SELECT id, linkedin_url, last_enriched_at, created_at, updated_at
                FROM leads WHERE id = ANY(%s)
            """, (ids,))
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            canon = pick_canonical(rows)
            dup_ids = [r["id"] for r in rows if r["id"] != canon["id"]]
            merge_group(cur, canon["id"], dup_ids, stats)

        print("\n=== merge stats ===")
        for k, v in stats.items():
            print(f"  {k}: {v}")

        # Flag conversation collisions — those deletes cascade lead_messages.
        cc = stats.get("lead_conversations:collisions_deleted", 0)
        if cc:
            print(f"\n  ⚠ {cc} conversation(s) deleted as collisions — their "
                  f"messages cascaded. (Same rep had a separate chat on both "
                  f"rows; canonical's was kept.)")

        if args.execute:
            # Verify no duplicates remain before enforcing uniqueness.
            cur.execute("""
                SELECT count(*) FROM (
                  SELECT 1 FROM leads WHERE member_urn IS NOT NULL
                  GROUP BY member_urn HAVING count(*) > 1) t
            """)
            remaining = cur.fetchone()[0]
            if remaining == 0:
                cur.execute(UNIQUE_INDEX_SQL)
                print(f"\nremaining dup groups: 0 → created {UNIQUE_INDEX_SQL}")
            else:
                print(f"\nremaining dup groups: {remaining} "
                      f"(--limit used?) → unique index NOT created")
            conn.commit()
            print("COMMITTED.")
        else:
            conn.rollback()
            print("\nDRY-RUN — rolled back, no changes written. "
                  "Re-run with --execute to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
