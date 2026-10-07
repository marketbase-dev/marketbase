#!/usr/bin/env python3
"""Propagate a hold tag from the members that carry it to EVERY member of the
same seed.

Staleness is a property of the SEED, not of the person. A seed detected before
the client's relevance cutoff makes its whole buying committee out of scope --
the CFO, the VPs, and the bench behind them alike.

The tag was applied the other way round once, and the failure was quiet and
expensive. A one-off script tagged the members that were `expansion:qualified`
at that moment -- the top N at each company -- and left the backup tier clean.
Re-tiering then saw N empty slots, promoted the untagged backups into them, and
the "stale" companies came back with a full hand-over set of second-choice
people. The signature is unmistakable once you look: the maximum number of held
members in any seed group was exactly the hand-over cap.

So: hold the seed, not the tier. Idempotent, and every write is auditable --
lead_tags inserts carry `tagged_by`/`notes`, and the removal trigger records
anything that later supersedes them.

    python3 propagate_seed_holds.py --client <Client> --tag expansion:stale --dry-run
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect  # noqa: E402

# Members are linked to the seeds that produced them by the seed profile URLs in
# the expansion source row; that array is the seed-group key.
SEED_GROUPS = """
WITH mem AS (
    SELECT l.id AS lead_id,
           (s.raw_data->'seed_linkedin_urls')::text AS seedkey,
           EXISTS (SELECT 1 FROM lead_tags t
                    WHERE t.lead_id = l.id AND t.tag = %(tag)s) AS held
    FROM leads l
    JOIN lead_sources s ON s.lead_id = l.id AND s.source_type = %(source_type)s
    WHERE s.raw_data ? 'seed_linkedin_urls'
),
held_seeds AS (
    SELECT seedkey FROM mem GROUP BY seedkey HAVING bool_or(held)
)
SELECT m.lead_id
FROM mem m
JOIN held_seeds h USING (seedkey)
WHERE NOT m.held
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--tag", default="expansion:stale",
                    help="the hold tag to propagate (default: expansion:stale)")
    ap.add_argument("--source-type", default="buying_committee_expansion")
    ap.add_argument("--notes", default=None,
                    help="notes for the new tag rows; defaults to the notes "
                         "already used on that tag, so the reason is preserved")
    ap.add_argument("--tagged-by", default="propagate_seed_holds")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with connect(args.client) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM lead_tags WHERE tag = %s", (args.tag,))
        before = cur.fetchone()[0]
        if not before:
            print(f"no lead carries {args.tag!r} — nothing to propagate from.")
            return 0

        notes = args.notes
        if notes is None:
            cur.execute("""SELECT notes, count(*) FROM lead_tags WHERE tag=%s AND notes IS NOT NULL
                           GROUP BY 1 ORDER BY 2 DESC LIMIT 1""", (args.tag,))
            row = cur.fetchone()
            notes = (row[0] if row else None) or f"propagated from a held seed ({args.tag})"

        cur.execute(SEED_GROUPS, {"tag": args.tag, "source_type": args.source_type})
        targets = [r[0] for r in cur.fetchall()]

        # what the hold currently costs, so the effect is visible before writing
        cur.execute(f"""
            SELECT count(*) FILTER (WHERE EXISTS (SELECT 1 FROM lead_tags t
                        WHERE t.lead_id = x.lead_id AND t.tag = 'expansion:qualified')),
                   count(*) FILTER (WHERE EXISTS (SELECT 1 FROM lead_tags t
                        WHERE t.lead_id = x.lead_id AND t.tag = 'expansion:backup')),
                   count(*)
            FROM ({SEED_GROUPS}) x
        """, {"tag": args.tag, "source_type": args.source_type})
        q, b, tot = cur.fetchone()
        print(f"{args.client}: {before:,} lead(s) already carry {args.tag!r}")
        print(f"  members of the same seeds that do NOT: {tot:,}")
        print(f"    currently expansion:qualified : {q:,}  <- would stop being handed over")
        print(f"    currently expansion:backup    : {b:,}")
        if args.dry_run:
            print("\nDRY RUN — nothing written.")
            return 0
        if not targets:
            print("nothing to do.")
            return 0

        cur.executemany("""INSERT INTO lead_tags (lead_id, tag, notes, tagged_by)
                           VALUES (%s,%s,%s,%s)
                           ON CONFLICT (lead_id, tag) DO NOTHING""",
                        [(lid, args.tag, notes, args.tagged_by) for lid in targets])
        conn.commit()
        cur.execute("SELECT count(*) FROM lead_tags WHERE tag = %s", (args.tag,))
        after = cur.fetchone()[0]
        print(f"\ntagged {after - before:,} more; {args.tag!r} now on {after:,} lead(s).")

        cur.execute(SEED_GROUPS, {"tag": args.tag, "source_type": args.source_type})
        left = len(cur.fetchall())
        print(f"verify — members of a held seed still untagged: {left}"
              f"{'  ✓' if left == 0 else '  <- UNEXPECTED'}")
        return 0 if left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
