#!/usr/bin/env python3
"""marketbase-merge-leads — fold duplicate leads into their keepers with
merge_lead() (schema/080). Deletes nothing.

The queue is lead_merge_queue: pairs the identity system already says are one
human (one lead owns a linkedin_urn identity, the other holds an open conflict
on it). A name or a vanity URL is never enough; merge_lead() refuses those.

DEFAULT IS A DRY RUN. The whole plan runs in one transaction that is rolled
back: every merge_lead() call, its moves, what stays on the dup, which keeper
columns get filled. It prints a plan digest. Nothing is written.

--execute needs the digest of the plan the owner approved. It re-plans, refuses
if the digest differs (the data moved since), then merges pair by pair, each in
its own transaction with: a snapshot of every row the merge touches written to
merge_snapshot_rows (in the database, never on disk), the merge, the
snapshot_ref on lead_merges, and asserted rowcounts.

Keeper: by default the lead that owns the URN identity (it was there first;
the others collided with it). Pass --pair DUP:KEEPER to choose otherwise, e.g.
the row that is a live Comp Intel target. Pairs run in the order given, so a
group of three is two --pair arguments onto the same keeper.

Usage:
  python3 merge_leads.py --client X                          # dry run, whole queue
  python3 merge_leads.py --client X --pair DUP:KEEPER ...    # dry run, chosen pairs
  python3 merge_leads.py --client X --with-migration         # before 080 is applied: plan, all rolled back
  python3 merge_leads.py --client X [--pair ...] --execute --plan <digest> --by <who approved>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

# Installed (~/.claude/tools/marketbase) schema/ sits beside the tools; in the repo it is one up.
MIGRATION = next((d / "080_lead_merge.sql" for d in (lib.SCHEMA_DIR, Path(__file__).resolve().parent.parent / "schema")
                  if (d / "080_lead_merge.sql").exists()), lib.SCHEMA_DIR / "080_lead_merge.sql")

# Every table merge_lead() touches, and the lead-id columns that tie a row to the pair.
SNAPSHOT = {
    "leads": ["id"],
    "lead_identities": ["lead_id"],
    "lead_identity_conflicts": ["lead_id", "owner_lead_id"],
    "enrichment_calls": ["lead_id"], "lead_sources": ["lead_id"],
    "lead_qualifications": ["lead_id"], "campaign_members": ["lead_id"],
    "lead_actions": ["lead_id"], "posts": ["poster_lead_id"],
    "post_engagements": ["lead_id"], "lead_tags": ["lead_id"],
    "lead_signals": ["lead_id"], "lead_conversations": ["lead_id"],
    "lead_meetings": ["lead_id"], "signals": ["subject_lead_id"],
    "assertions": ["subject_lead_id"], "voices": ["lead_id"],
    "lead_touches": ["lead_id"], "crm_leads": ["lead_id"], "crm_contacts": ["lead_id"],
}


# Relationship history: what a lead's row has accumulated with people and clients.
# enrichment_calls and identities are lookups, not history, so they do not count.
HISTORY = [("campaign_members", "lead_id"), ("lead_conversations", "lead_id"),
           ("lead_meetings", "lead_id"), ("lead_touches", "lead_id"), ("lead_actions", "lead_id"),
           ("lead_qualifications", "lead_id"), ("crm_leads", "lead_id"), ("crm_contacts", "lead_id"),
           ("lead_tags", "lead_id"), ("lead_sources", "lead_id"), ("post_engagements", "lead_id"),
           ("posts", "poster_lead_id"), ("voices", "lead_id"), ("assertions", "subject_lead_id"),
           ("lead_signals", "lead_id"), ("signals", "subject_lead_id")]


def history(cur, lid: str) -> int:
    # A dup's own duplicate tag pair is a finding about the row, not history.
    not_dup_pair = {"lead_tags": " AND tag NOT IN ('flag:duplicate_lead', 'expansion:do_not_push')"}
    cur.execute("SELECT " + " + ".join(f"(SELECT count(*) FROM {t} WHERE {c} = %(l)s{not_dup_pair.get(t, '')})"
                                       for t, c in HISTORY), {"l": lid})
    return cur.fetchone()[0]


def queue_pairs(cur) -> list[tuple[str, str, str]]:
    """Per URN group: keeper = most relationship history, then the oldest row
    (the owner's rule, 2026-11-04: the keeper is the row with the history, not the identity
    owner). The owner merges first when it is not the keeper, so the others'
    findings are handed to the keeper before their own merges."""
    cur.execute("""SELECT value, owner_lead_id::text, owner_created_at, other_lead_id::text, other_created_at
                     FROM lead_merge_queue ORDER BY value, other_created_at, other_lead_id""")
    groups: dict[str, dict[str, object]] = {}
    for value, owner, owner_at, other, other_at in cur.fetchall():
        g = groups.setdefault(value, {"owner": owner, "members": {owner: owner_at}})
        g["members"][other] = other_at
    pairs = []
    for value, g in groups.items():
        rank = {lid: (history(cur, lid), at) for lid, at in g["members"].items()}
        keeper = sorted(rank, key=lambda lid: (-rank[lid][0], rank[lid][1], lid))[0]
        why = f"history {rank[keeper][0]}" + ("" if keeper == g["owner"] else ", not the identity owner")
        order = sorted((m for m in g["members"] if m != keeper), key=lambda m: (m != g["owner"], rank[m][1]))
        pairs += [(m, keeper, why) for m in order]
    return pairs


def describe(cur, lid: str) -> str:
    cur.execute("SELECT name, linkedin_url, created_at::date FROM leads WHERE id = %s", (lid,))
    r = cur.fetchone()
    return f"{lid}  {r[0] or '?'}  {r[1] or '(no URL)'}  created {r[2]}" if r else f"{lid}  (missing)"


def snapshot(cur, sid: str, dup: str, keeper: str) -> int:
    n = 0
    for tbl, cols in SNAPSHOT.items():
        where = " OR ".join(f"t.{c} = ANY(%(ids)s::uuid[])" for c in cols)
        cur.execute(f"INSERT INTO merge_snapshot_rows (snapshot_id, tbl, row_data) "
                    f"SELECT %(sid)s, %(tbl)s, to_jsonb(t) FROM {tbl} t WHERE {where}",
                    {"sid": sid, "tbl": tbl, "ids": [dup, keeper]})
        n += cur.rowcount
    return n


def plan(cur, pairs) -> tuple[list[dict], list[tuple[str, str, str]]]:
    """Run every merge in savepoints; the caller rolls back. Returns results and failures."""
    out, failed = [], []
    for dup, keeper in pairs:
        cur.execute("SAVEPOINT pair")
        try:
            cur.execute("SELECT merge_lead(%s, %s, 'merge_leads plan')", (dup, keeper))
            m = cur.fetchone()[0]
            m = m if isinstance(m, dict) else json.loads(m)
            out.append({"dup": dup, "keeper": keeper, "moved": m["moved"], "stayed": m["stayed"],
                        "filled": m["filled"], "cleared": m["cleared"],
                        "replaced": m.get("replaced", {})})
            cur.execute("RELEASE SAVEPOINT pair")
        except Exception as e:  # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT pair")
            failed.append((dup, keeper, str(e).splitlines()[0]))
    return out, failed


def digest(results: list[dict]) -> str:
    blob = json.dumps(results, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def show(cur, results, failed, why) -> None:
    for r in results:
        print(f"\n  dup    {describe(cur, r['dup'])}")
        print(f"  keeper {describe(cur, r['keeper'])}   ({why.get((r['dup'], r['keeper']), '')})")
        print(f"    moves:  {', '.join(f'{k} {v}' for k, v in r['moved'].items()) or 'none'}")
        stayed = {k: len(v) for k, v in r["stayed"].items()}
        print(f"    stays on the dup: {', '.join(f'{k} {v}' for k, v in stayed.items()) or 'nothing'}")
        print(f"    keeper filled: {', '.join(r['filled']) or 'nothing'};  dup cleared: {', '.join(r['cleared']) or 'nothing'}")
        if r["replaced"]:
            print(f"    keeper's URN-form URL replaced by the dup's vanity URL: {r['replaced'].get('linkedin_url')}")
    for d, k, err in failed:
        print(f"\n  REFUSED {d} -> {k}: {err}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge duplicate leads with merge_lead(); deletes nothing.")
    ap.add_argument("--client", required=True)
    ap.add_argument("--pair", action="append", default=[], metavar="DUP:KEEPER",
                    help="a pair to merge, in order; default is the whole lead_merge_queue")
    ap.add_argument("--with-migration", action="store_true",
                    help="dry run only: apply schema/080 inside the rolled-back transaction first")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--plan", help="the digest of the approved dry run (required with --execute)")
    ap.add_argument("--by", help="who approved the plan; recorded as lead_merges.merged_by")
    args = ap.parse_args()
    if args.execute and (not args.plan or not args.by or args.with_migration):
        sys.exit("--execute needs --plan <digest> from an approved dry run and --by <who approved>, and never --with-migration")

    conn = lib.connect(args.client)
    cur = conn.cursor()
    cur.execute("SET lock_timeout = '10s'")
    if args.with_migration:
        cur.execute(MIGRATION.read_text())
        print(f"(080 applied inside this transaction; it is rolled back)")

    cur.execute("""SELECT kind, count(*) FROM lead_identity_conflicts
                    WHERE resolved_by_merge IS NULL GROUP BY kind ORDER BY kind""")
    print("open conflicts by kind: " + (", ".join(f"{k} {n}" for k, n in cur.fetchall()) or "none")
          + "  (only linkedin_urn pairs are mergeable)")
    chosen = [tuple(p.split(":", 1)) + ("chosen by --pair",) for p in args.pair]
    triples = chosen or queue_pairs(cur)
    why = {(d, k): w for d, k, w in triples}
    pairs = [(d, k) for d, k, _ in triples]
    if not pairs:
        print("Nothing to merge: lead_merge_queue is empty.")
        conn.rollback()
        return 0

    results, failed = plan(cur, pairs)
    d = digest(results)
    conn.rollback()

    print(f"{args.client}: {len(pairs)} pair(s), {len(results)} planned, {len(failed)} refused")
    show(cur, results, failed, why)
    print(f"\nplan digest: {d}")

    if not args.execute:
        print("Dry run: nothing written. To merge, after approval: --execute --plan " + d)
        return 0
    if failed:
        sys.exit("Refusing to execute: some pairs were refused. Drop them with explicit --pair arguments.")
    if d != args.plan:
        sys.exit(f"Refusing to execute: the plan is now {d}, not the approved {args.plan}. Re-run the dry run.")

    for r in results:
        dup, keeper = r["dup"], r["keeper"]
        cur.execute("SELECT gen_random_uuid()::text")
        sid = cur.fetchone()[0]
        n_snap = snapshot(cur, sid, dup, keeper)
        cur.execute("SELECT merge_lead(%s, %s, %s)", (dup, keeper, args.by))
        m = cur.fetchone()[0]
        m = m if isinstance(m, dict) else json.loads(m)
        cur.execute("UPDATE lead_merges SET snapshot_ref = %s WHERE id = %s AND snapshot_ref IS NULL",
                    (f"merge_snapshot_rows:{sid}", m["id"]))
        if cur.rowcount != 1:
            raise RuntimeError(f"expected 1 lead_merges row for {dup}, got {cur.rowcount}")
        cur.execute("SELECT merged_into::text FROM leads WHERE id = %s", (dup,))
        if cur.fetchone()[0] != keeper:
            raise RuntimeError(f"{dup} is not merged into {keeper} after merge_lead")
        conn.commit()
        print(f"MERGED {dup} into {keeper}  (lead_merges {m['id']}, snapshot {sid}, {n_snap} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
