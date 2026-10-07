#!/usr/bin/env python3
"""marketbase-dedupe-leads — merge duplicate person rows onto one canonical lead.

Two grouping keys, for two ways one person became two rows.

--key urn (default). `leads` was keyed on `linkedin_url` (the URL string), so
the same real person ingested under a vanity URL (/in/johndoe) AND a URN
URL (/in/ACoAA…) became two rows. They share the same LinkedIn member URN
(`leads.member_urn`, added in migration 027), which IS the real identity.

--key url. The same person under one HANDLE, stored in two or three encodings:
a name carrying an emoji, a ™, or an accent arrives literal from one source and
percent-encoded from another, and the two encoders disagree on hex case, so
`%C3%B1` and `%c3%b1` are a third row again. member_urn does not catch these —
these rows frequently have no URN at all, which is exactly why they were never
reconciled. Measured 2026-10-07 across two instances: 74 + 9 duplicate groups,
85 extra lead rows, and 22 people being MONITORED TWICE, which means duplicated
webhooks on live client workspaces and a connection-tracking set overstating
itself by 22.

url mode merges, then rewrites every stored URL to its normalised form — the
rewrite is the half that stops the duplicate re-forming on the next ingest, and
it belongs in the same transaction because linkedin_url is UNIQUE NOT NULL.
It depends on `lib.normalize_linkedin_url` percent-decoding; run
`tools/test_normalize_url.py` first if that function has been touched.

ONE class is reported and never merged: two rows holding the same member URN
differing only in CASE. URN case is significant — a lowercased URN 404s — so
the normaliser keeps them apart by design and a human picks the survivor, the
case-preserved one. See urn_case_collisions().

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
  python3 dedupe_leads.py --client Acme                     # dry-run, by urn
  python3 dedupe_leads.py --client Acme --key url           # dry-run, by url
  python3 dedupe_leads.py --client Acme --key url --execute # commit
  python3 dedupe_leads.py --client Acme --limit 20          # first 20 groups
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, normalize_linkedin_url


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
# name/title/company that only the duplicate had is not lost. (linkedin_url
# stays the canonical's.)
#
# member_urn is in this list, which it did not need to be when urn was the only
# grouping key — the group was BUILT on member_urn, so every row had the same
# one. Grouping on the URL instead, the twins routinely disagree: one row was
# URN-captured and the other was not. Leaving it out would delete the captured
# URN along with the row holding it.
FILL_COLS = [
    "member_urn", "linkedin_urn", "public_id", "name", "headline", "current_title",
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


# A member URN sitting in the linkedin_url FIELD: ~39 chars of mixed-case
# base64-ish with no hyphens, conventionally starting "AC". Used only to spot a
# LOWERCASED one, which is unusable — see assert_no_lowercased_urn.
_URN_IN_URL_RE = re.compile(r"/in/(AC[A-Za-z0-9_-]{25,})/?$", re.I)


def url_groups(cur, decode) -> list[tuple[str, list]]:
    """Group leads by the NORMALISED url. Grouping happens in Python because
    the normaliser is Python — which is the point: the DB cannot be the judge
    of a key whose definition lives in the library every writer imports."""
    cur.execute("SELECT id, linkedin_url FROM leads "
                "WHERE COALESCE(linkedin_url,'') <> '' ORDER BY created_at")
    buckets: dict[str, list] = {}
    for lead_id, url in cur.fetchall():
        buckets.setdefault(decode(url), []).append(lead_id)
    return [(k, ids) for k, ids in sorted(buckets.items()) if len(ids) > 1]


def urn_case_collisions(cur) -> list[tuple[str, list[tuple]]]:
    """Rows whose URLs are the same member URN differing only in CASE.

    Reported, never merged. These are almost certainly one person, but the
    normaliser deliberately keeps them apart: URN case is significant, so the
    lowercased form 404s against saleleads / Fresh. Deciding which row dies
    also decides which identity survives, and if the lowercased row wins the
    survivor's URL is permanently dead. That is a judgement call on a live
    monitored target, so it goes to a human with the rule attached:

        KEEP THE CASE-PRESERVED ROW.

    Case-insensitive grouping is safe HERE because nothing is written from it.
    """
    cur.execute("SELECT id, linkedin_url, member_urn FROM leads "
                "WHERE COALESCE(linkedin_url,'') <> '' ORDER BY created_at")
    buckets: dict[str, list[tuple]] = {}
    for lead_id, url, urn in cur.fetchall():
        m = _URN_IN_URL_RE.search(url or "")
        if m:
            buckets.setdefault(m.group(1).lower(), []).append((lead_id, url, urn))
    return [(k, v) for k, v in sorted(buckets.items()) if len(v) > 1]


def lowercased_urns(cur) -> set:
    """Lead ids whose linkedin_url holds an ALL-LOWERCASE member URN."""
    cur.execute("SELECT id, linkedin_url FROM leads "
                "WHERE COALESCE(linkedin_url,'') <> ''")
    out = set()
    for lead_id, url in cur.fetchall():
        m = _URN_IN_URL_RE.search(url or "")
        # A real URN is mixed-case. All-lowercase means someone folded it.
        if m and m.group(1) == m.group(1).lower():
            out.add(lead_id)
    return out


def assert_no_lowercased_urn(cur, baseline: set) -> None:
    """Tripwire: this run may not CREATE an all-lowercase member URN.

    Not a defence against this tool, which never changes case — a defence
    against the next change to the normaliser. "Normalise the URL" reads like
    one operation and is two: percent-decoding is safe because encoding case
    is insignificant for the byte it encodes, while lowercasing is unsafe
    because handle and URN case are significant. Anyone who conflates them
    lowercases the path, every URN-form URL in the DB dies at once, and
    nothing fails loudly — the strings still look like URLs. This fails
    loudly, inside the transaction, before the commit.

    Measured against a BASELINE taken before the run, not against zero. One
    instance already holds a pre-existing lowercased URN (the other half of a
    reported case collision), and failing on it would make url mode
    permanently unrunnable there — a tripwire nobody can get past stops
    protecting anything and just gets deleted. Pre-existing damage is the
    collision report's business; this guard's business is new damage.
    """
    now = lowercased_urns(cur)
    created = now - baseline
    if created:
        cur.execute("SELECT id, linkedin_url FROM leads WHERE id = ANY(%s)",
                    (list(created)[:5],))
        sample = ", ".join(f"{i}:{u}" for i, u in cur.fetchall())
        sys.exit(f"ABORT: this run created {len(created)} all-lowercase member "
                 f"URN(s) in linkedin_url, which 404 against the people APIs. "
                 f"Sample: {sample}")
    if baseline:
        print(f"  note: {len(baseline)} pre-existing lowercased URN(s) left as "
              f"found — see the case-collision report above.")


def pick_canonical(rows: list[dict]) -> dict:
    """Choose the survivor. Prefer the row that HOLDS the member_urn, then a
    URN-form linkedin_url (/in/AC…, the stable form), then the most-enriched /
    most-recently-updated / oldest row.

    member_urn ranks first because it is the one column a merge cannot
    reconstruct. A URL string can be re-derived from the person's page; a URN
    costs a paid capture, and for a live monitored target losing it breaks the
    link between the lead and the webhooks already firing on it. In url-keyed
    mode the group members differ only in URL encoding, so URN presence is
    usually the ONLY thing separating them -- and whichever row happens to be
    freshest is not the one to keep if the other is the one with an identity.
    (COALESCE-fill below copies the URN onto the survivor either way; this
    ordering means the survivor is the row that already had it, so a fill
    failure cannot lose it.)"""
    def key(r):
        has_urn = 1 if r.get("member_urn") else 0
        urn_form = 1 if "/in/AC" in (r["linkedin_url"] or "") else 0
        return (
            has_urn,
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
    #
    # Per column, take the freshest NON-NULL value across all duplicates. The
    # previous form selected whole rows `ORDER BY updated_at DESC` with no
    # LIMIT, so for a group of three Postgres was free to join any duplicate
    # row and the fill was nondeterministic — and worse, a column was filled
    # only from whichever row won, so a value held solely by the other
    # duplicate was dropped. Harmless while every group had exactly two rows;
    # three-row groups are real (one per instance in the 2026-10-07 scan, both
    # of them people we monitor).
    set_clause = ", ".join(f"{c} = COALESCE(c.{c}, d.{c})" for c in FILL_COLS)
    pick = ", ".join(
        f"(array_remove(array_agg({c} ORDER BY updated_at DESC), NULL))[1] AS {c}"
        for c in FILL_COLS)
    cur.execute(f"""
        UPDATE leads c SET {set_clause}
        FROM (SELECT {pick} FROM leads WHERE id = ANY(%s)) d
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
    p = argparse.ArgumentParser(description="Merge duplicate person rows.")
    p.add_argument("--client", required=True)
    p.add_argument("--execute", action="store_true",
                   help="Commit the merge (default: dry-run + rollback).")
    p.add_argument("--limit", type=int, help="Process only the first N groups.")
    p.add_argument("--key", choices=("urn", "url"), default="urn",
                   help="urn (default): group by member_urn. "
                        "url: group by the NORMALISED linkedin_url, then "
                        "rewrite every row to its normalised form.")
    args = p.parse_args()

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"[{mode}] dedupe leads by {args.key} for {args.client}\n")

    conn = connect(args.client)
    with conn.cursor() as cur:
        assert_child_set_current(cur)

        if args.key == "url":
            lowercase_baseline = lowercased_urns(cur)
            groups = url_groups(cur, normalize_linkedin_url)
            # Report-only, and printed BEFORE the merge so it is read even if
            # the operator stops at the dry-run. These rows do NOT appear in
            # `groups` — the normaliser keeps URN case apart on purpose.
            collisions = urn_case_collisions(cur)
            if collisions:
                print(f"\n⚠ {len(collisions)} member-URN case collision(s) — "
                      f"NOT merged, decide by hand, KEEP THE CASE-PRESERVED ROW:")
                for key, rows in collisions:
                    print(f"    urn {key[:18]}…")
                    for lead_id, url, urn in rows:
                        preserved = "case-preserved" if url != url.lower() else "LOWERCASED"
                        print(f"      lead {lead_id}  urn={'yes' if urn else 'no ':3}"
                              f"  {preserved:14} {url}")
                print()
        else:
            # Group only on URN-SHAPED values. An empty string is not an
            # identity and neither is a truncated handle, but both are NOT
            # NULL, so `WHERE member_urn IS NOT NULL` would hand them to the
            # merger as a group of "the same person" and it would delete
            # unrelated humans with a clean exit code.
            #
            # member_urn is in fact clean on both instances measured
            # (0 empty, 0 malformed, 0 unsafe groups), and the pollution found
            # during review was in the ADJACENT column, linkedin_urn: 1,707
            # rows there hold a vanity handle or a truncated one. That is the
            # reason for the guard rather than a reason against it — the two
            # column names are one word apart, nothing but this predicate
            # would stop the swap, and the failure mode is silent deletion.
            #
            # ANCHORED: unanchored would also accept 'junk-ACoAA…'. Measured
            # identical on real data (2,720 rows, 50 groups either way, and
            # nothing matches unanchored but not anchored), so the stricter
            # form costs nothing.
            cur.execute("""
                SELECT member_urn, array_agg(id ORDER BY created_at) AS ids
                FROM leads
                WHERE member_urn ~ '^AC[A-Za-z0-9_-]{15,}$'
                GROUP BY member_urn
                HAVING count(*) > 1
                ORDER BY member_urn
            """)
            groups = cur.fetchall()

            # Report what the guard excluded, so pollution is visible rather
            # than silently skipped.
            cur.execute("""
                SELECT count(*) FROM leads
                WHERE member_urn IS NOT NULL
                  AND member_urn !~ '^AC[A-Za-z0-9_-]{15,}$'
            """)
            skipped = cur.fetchone()[0]
            if skipped:
                print(f"  note: {skipped} lead(s) hold a non-URN-shaped "
                      f"member_urn and are EXCLUDED from grouping.")

        if args.limit:
            groups = groups[:args.limit]
        print(f"duplicate {args.key} groups: {len(groups)}"
              + (f" (limited to {args.limit})" if args.limit else ""))
        if not groups and args.key == "urn":
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

        for _group_key, ids in groups:
            # Re-fetch full rows for canonical selection.
            cur.execute("""
                SELECT id, linkedin_url, member_urn, last_enriched_at,
                       created_at, updated_at
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

        # ---- url mode: rewrite every stored URL to its normalised form.
        #
        # The merge alone is cosmetic without this. Dropping the encoded twin
        # while the survivor keeps its own encoded string means the next ingest
        # of the literal form — the form LinkedIn shows and a human pastes —
        # fails to match and inserts the duplicate again. Rows with no twin
        # need it too: they are the duplicate waiting to happen.
        #
        # AFTER the merges, and in the same transaction. linkedin_url is UNIQUE
        # NOT NULL (schema/001), so rewriting first would hit the constraint on
        # exactly the rows the merge is there to collapse.
        if args.key == "url":
            cur.execute("SELECT id, linkedin_url FROM leads "
                        "WHERE COALESCE(linkedin_url,'') <> ''")
            pending = [(i, u, normalize_linkedin_url(u)) for i, u in cur.fetchall()]
            pending = [(i, u, n) for i, u, n in pending if n and n != u]
            for lead_id, _old, new_url in pending:
                cur.execute("UPDATE leads SET linkedin_url=%s, updated_at=now() "
                            "WHERE id=%s", (new_url, lead_id))
            stats["urls:rewritten"] = len(pending)
            for lead_id, old_url, new_url in pending[:10]:
                print(f"  rewrite lead {lead_id}: {old_url}\n"
                      f"                 -> {new_url}")
            if len(pending) > 10:
                print(f"  … and {len(pending) - 10} more")

            # Cheap proof the rewrite converged, before anything commits.
            cur.execute("SELECT id, linkedin_url FROM leads "
                        "WHERE COALESCE(linkedin_url,'') <> ''")
            still = [(i, u) for i, u in cur.fetchall()
                     if normalize_linkedin_url(u) != u]
            if still:
                sys.exit(f"ABORT: {len(still)} url(s) still not in normal form "
                         f"after the rewrite, e.g. {still[0]}")
            assert_no_lowercased_urn(cur, lowercase_baseline)

        if args.execute:
            # Verify no duplicates remain before enforcing uniqueness.
            if args.key == "url":
                # No index to create: linkedin_url is already UNIQUE NOT NULL
                # (schema/001). What the rewrite changes is WHICH strings
                # collide under it, so the existing constraint now does the
                # work it was always meant to.
                remaining = len(url_groups(cur, normalize_linkedin_url))
                print(f"\nremaining dup groups: {remaining}")
            else:
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
