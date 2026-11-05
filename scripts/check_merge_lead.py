#!/usr/bin/env python3
"""Prove merge_lead() and its refusals (schema/080) on a fixture. Every write runs
inside ONE transaction that is always rolled back.

What it proves:
  * the registry covers every FK column to leads, and each moved column has its
    merged_from_ column and both refusal triggers (a new lead_id table that skips
    this fails here, not in production);
  * lead_identity_claim() writes a lead_identity_conflicts row on the duplicate
    outcome and still lands the tag pair; the pair shows in lead_merge_queue;
  * merge_lead() refuses: no shared URN, the same row, an already-merged row;
  * a merge re-points every one of the 17 moved columns and stamps merged_from_,
    leaves colliding rows (and the dup's own duplicate tag pair) on the dup, moves
    identities as non-primary, clears the dup's identity columns, fills the
    keeper's NULLs without overwriting, resolves the conflict, records lead_merges,
    and changes no table's row count;
  * after it, an insert or update aimed at the merged lead is refused.

Usage:
  python3 scripts/check_merge_lead.py                 # MARKETBASE_URL (CI)
  python3 scripts/check_merge_lead.py --url postgresql://...
  python3 scripts/check_merge_lead.py --instance <name>
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

REFUSED = "23514"
BAD_ARG = "22023"
URN = "ACoAA" + "MERGE" * 7 + "01"
URN_OTHER = "ACoAA" + "MERGE" * 7 + "02"
IN = "https://www.linkedin.com/in/"

# The 17 columns merge_lead() moves, plus the ones it handles itself.
MOVES = [
    ("enrichment_calls", "lead_id"), ("lead_sources", "lead_id"),
    ("lead_qualifications", "lead_id"), ("campaign_members", "lead_id"),
    ("lead_actions", "lead_id"), ("posts", "poster_lead_id"),
    ("post_engagements", "lead_id"), ("lead_tags", "lead_id"),
    ("lead_signals", "lead_id"), ("lead_conversations", "lead_id"),
    ("lead_meetings", "lead_id"), ("signals", "subject_lead_id"),
    ("assertions", "subject_lead_id"), ("voices", "lead_id"),
    ("lead_touches", "lead_id"), ("crm_leads", "lead_id"), ("crm_contacts", "lead_id"),
]
SPECIAL = {("lead_identities", "lead_id"), ("lead_identity_conflicts", "lead_id"),
           ("lead_identity_conflicts", "owner_lead_id"), ("lead_merges", "dup_id"),
           ("lead_merges", "keeper_id"), ("leads", "merged_into")}


def _connect(args):
    if args.instance:
        import lib
        return lib.connect(args.instance)
    url = args.url or os.environ.get("MARKETBASE_URL")
    if not url:
        sys.exit("No database. Pass --url, --instance, or set MARKETBASE_URL.")
    try:
        import psycopg
        return psycopg.connect(url, autocommit=False)
    except ImportError:
        import psycopg2
        conn = psycopg2.connect(url)
        conn.autocommit = False
        return conn


def _sqlstate(e) -> str | None:
    return getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)


def run(conn) -> tuple[int, list[str]]:
    cur = conn.cursor()
    fails: list[str] = []
    total = 0

    def check(name, cond, detail=""):
        nonlocal total
        total += 1
        print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  ' + str(detail)}")
        if not cond:
            fails.append(name)

    def one(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchone()[0]

    def raises(sql, params=()):
        cur.execute("SAVEPOINT sp")
        try:
            cur.execute(sql, params)
        except Exception as e:  # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT sp")
            return _sqlstate(e)
        cur.execute("RELEASE SAVEPOINT sp")
        return None

    # ── registry ──────────────────────────────────────────────────────────────
    cur.execute("""SELECT c.conrelid::regclass::text, a.attname
                     FROM pg_constraint c
                     JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
                    WHERE c.contype = 'f' AND c.confrelid = 'leads'::regclass""")
    fk = set(cur.fetchall())
    unlisted = fk - set(MOVES) - SPECIAL
    check("every FK column to leads is classified (moved or special)", not unlisted, sorted(unlisted))
    for tbl, col in MOVES:
        has = one("SELECT count(*) FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
                  (tbl, "merged_from_" + col))
        trg = one("SELECT count(*) FROM pg_trigger WHERE tgrelid=%s::regclass AND tgname IN "
                  "('trg_refuse_merged_lead_ins','trg_refuse_merged_lead_upd')", (tbl,))
        if has != 1 or trg != 2:
            check(f"{tbl}.{col} has merged_from_{col} and both refusal triggers", False, (has, trg))
    check("each moved column has merged_from_ and both refusal triggers",
          not any(f.endswith("both refusal triggers") for f in fails))

    # ── fixture ───────────────────────────────────────────────────────────────
    def lead(handle, urn=None, **cols):
        # The URN is filed through the claim, the way the tools file one, not as
        # leads.member_urn: some instances carry a unique index on member_urn, so
        # two rows there can share a URN only in lead_identities.
        names = ["linkedin_url", "name"] + list(cols)
        vals = [IN + handle, "Merge Fixture " + handle] + list(cols.values())
        cur.execute(f"INSERT INTO leads ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(vals))}) RETURNING id",
                    vals)
        lid = cur.fetchone()[0]
        if urn:
            cur.execute("SELECT lead_identity_claim(%s, 'linkedin_urn', %s, 'check_merge_lead')", (lid, urn))
        return lid

    keeper = lead("merge-fixture-keeper", URN, current_title="Keeper title")
    dup = lead("merge-fixture-dup", URN, current_title="Dup title", city="Dup city",
               email="merge-fixture@example.invalid")
    loner = lead("merge-fixture-loner", URN_OTHER)

    cur.execute("SELECT kind, value, owner_lead_id FROM lead_identity_conflicts WHERE lead_id=%s", (dup,))
    conf = cur.fetchall()
    check("claim on a duplicate URN writes a conflict row naming the owner",
          (("linkedin_urn", URN, keeper) in [tuple(r) for r in conf]), conf)
    check("the tag pair still lands on the duplicate",
          one("SELECT count(*) FROM lead_tags WHERE lead_id=%s AND tag IN "
              "('flag:duplicate_lead','expansion:do_not_push')", (dup,)) == 2)
    check("lead_merge_queue shows the pair",
          one("SELECT count(*) FROM lead_merge_queue WHERE owner_lead_id=%s AND other_lead_id=%s",
              (keeper, dup)) == 1)

    camp = one("INSERT INTO campaigns (name, channel) VALUES ('merge fixture', 'manual') RETURNING id")
    op = one("INSERT INTO outbound_operators (display_name, channel, channel_identifier, unipile_account_id) "
             "VALUES ('merge fixture', 'linkedin', 'merge-fixture', 'merge-fixture') RETURNING id")
    post = one("INSERT INTO posts (post_urn) VALUES ('urn:li:activity:merge-fixture') RETURNING id")

    def children(lid, suffix):
        """One row in every moved table for lid. Rows marked * collide across the pair."""
        cur.execute("INSERT INTO enrichment_calls (api, endpoint, lead_id) VALUES ('fixture', %s, %s)", (suffix, lid))
        cur.execute("INSERT INTO lead_sources (lead_id, source_type, source_label) VALUES (%s, 'manual_add', %s)",
                    (lid, suffix))
        cur.execute("INSERT INTO lead_qualifications (lead_id, qualifier_name, qualifier_version, qualified) "
                    "VALUES (%s, 'fixture', %s, true)", (lid, suffix))
        cur.execute("INSERT INTO campaign_members (lead_id, campaign_id) VALUES (%s, %s)", (lid, camp))       # *
        cur.execute("INSERT INTO lead_actions (lead_id, action, requested_by) VALUES (%s, 'archive', %s)",
                    (lid, suffix))
        cur.execute("INSERT INTO posts (post_urn, poster_lead_id) VALUES (%s, %s)",
                    ("urn:li:activity:merge-fixture-" + suffix, lid))
        cur.execute("INSERT INTO post_engagements (post_id, lead_id, engagement_type) VALUES (%s, %s, 'reaction')",
                    (post, lid))
        cur.execute("INSERT INTO lead_tags (lead_id, tag) VALUES (%s, 'fixture:shared')", (lid,))          # *
        cur.execute("INSERT INTO lead_tags (lead_id, tag) VALUES (%s, %s)", (lid, "fixture:" + suffix))
        cur.execute("INSERT INTO lead_signals (lead_id, enricher_name, enricher_version, payload) "
                    "VALUES (%s, 'fixture', '1', '{}')", (lid,))                                              # *
        cur.execute("INSERT INTO lead_conversations (operator_id, lead_id, channel, provider_chat_id) "
                    "VALUES (%s, %s, 'linkedin', %s)", (op, lid, suffix))                                     # *
        cur.execute("INSERT INTO lead_meetings (lead_id, scheduled_at) VALUES (%s, now())", (lid,))
        cur.execute("INSERT INTO signals (enricher_name, payload, subject_lead_id) VALUES ('fixture', '{}', %s)",
                    (lid,))
        cur.execute("INSERT INTO assertions (namespace, key, asserted_by, subject_lead_id) "
                    "VALUES ('fixture', 'shared', 'fixture', %s)", (lid,))                                    # *
        cur.execute("INSERT INTO assertions (namespace, key, asserted_by, subject_lead_id) "
                    "VALUES ('fixture', %s, 'fixture', %s)", (suffix, lid))
        cur.execute("INSERT INTO voices (voice_group, selected_by, added_by, added_reason, lead_id) "
                    "VALUES ('industry', 'manual', 'fixture', 'fixture', %s)", (lid,))                        # *
        cur.execute("INSERT INTO lead_touches (lead_id, touched_at, rep, kind, source) "
                    "VALUES (%s, now(), 'fixture', 'call', 'fixture')", (lid,))
        cur.execute("INSERT INTO crm_leads (id, lead_id) VALUES (%s, %s)", ("fixture-lead-" + suffix, lid))
        cur.execute("INSERT INTO crm_contacts (id, lead_id) VALUES (%s, %s)", ("fixture-contact-" + suffix, lid))

    children(keeper, "k")
    children(dup, "d")
    # colliding per table: campaign_members, lead_tags(shared), lead_signals, assertions(shared), voices
    # (lead_conversations differ by provider_chat_id but collide on (operator_id, lead_id, channel))
    COLLIDE = {"campaign_members.lead_id": 1, "lead_tags.lead_id": 3,   # shared + the duplicate pair
               "lead_signals.lead_id": 1, "lead_conversations.lead_id": 1,
               "assertions.subject_lead_id": 1, "voices.lead_id": 1}

    # ── refusals ──────────────────────────────────────────────────────────────
    check("refuses a pair with no shared URN",
          raises("SELECT merge_lead(%s, %s, 'fixture')", (loner, keeper)) == BAD_ARG)
    check("refuses the same row", raises("SELECT merge_lead(%s, %s, 'fixture')", (dup, dup)) == BAD_ARG)

    counts_before = {}
    for tbl in {t for t, _ in MOVES} | {"lead_identities", "leads"}:
        counts_before[tbl] = one(f"SELECT count(*) FROM {tbl}")
    dup_ids_before = {}
    for tbl, col in MOVES:
        cur.execute(f"SELECT id FROM {tbl} WHERE {col} = %s", (dup,))
        dup_ids_before[f"{tbl}.{col}"] = {r[0] for r in cur.fetchall()}

    # ── the merge ─────────────────────────────────────────────────────────────
    cur.execute("SELECT merge_lead(%s, %s, 'check_merge_lead')", (dup, keeper))
    m = cur.fetchone()[0]
    m = m if isinstance(m, dict) else json.loads(m)
    moved, stayed = m["moved"], m["stayed"]

    for tbl, col in MOVES:
        key = f"{tbl}.{col}"
        n_dup = len(dup_ids_before[key])
        want_stay = COLLIDE.get(key, 0)
        left = one(f"SELECT count(*) FROM {tbl} WHERE {col} = %s", (dup,))
        stamped = one(f"SELECT count(*) FROM {tbl} WHERE {col} = %s AND merged_from_{col} = %s", (keeper, dup))
        ok = (left == want_stay and stamped == n_dup - want_stay
              and moved.get(key, 0) == n_dup - want_stay and len(stayed.get(key, [])) == want_stay)
        check(f"{key}: {n_dup - want_stay} moved and stamped, {want_stay} stayed", ok,
              dict(left=left, stamped=stamped, moved=moved.get(key), stayed=stayed.get(key)))

    check("no table's row count changed",
          all(one(f"SELECT count(*) FROM {t}") == n for t, n in counts_before.items()))
    check("the dup's duplicate tag pair stayed on the dup; the keeper is not flagged",
          one("SELECT count(*) FROM lead_tags WHERE lead_id=%s AND tag IN "
              "('flag:duplicate_lead','expansion:do_not_push')", (dup,)) == 2
          and one("SELECT count(*) FROM lead_tags WHERE lead_id=%s AND tag IN "
                  "('flag:duplicate_lead','expansion:do_not_push')", (keeper,)) == 0)
    check("the dup's identities moved to the keeper as non-primary",
          one("SELECT count(*) FROM lead_identities WHERE lead_id=%s", (dup,)) == 0
          and one("SELECT count(*) FROM lead_identities WHERE lead_id=%s AND value=%s AND NOT is_primary "
                  "AND merged_from_lead_id=%s", (keeper, IN + "merge-fixture-dup", dup)) == 1)
    cur.execute("SELECT merged_into, linkedin_url, linkedin_urn, member_urn, public_id FROM leads WHERE id=%s", (dup,))
    d = cur.fetchone()
    check("the dup is marked merged and holds no URL/URN", d[0] == keeper and not any(d[1:]), d)
    check("lead_merges.cleared keeps the dup's URL",
          m["cleared"].get("linkedin_url") == IN + "merge-fixture-dup", m["cleared"])
    cur.execute("SELECT current_title, city, email, linkedin_url FROM leads WHERE id=%s", (keeper,))
    k = cur.fetchone()
    check("the keeper's NULLs are filled and its set values kept",
          k == ("Keeper title", "Dup city", "merge-fixture@example.invalid", IN + "merge-fixture-keeper"), k)
    check("the pair's conflict is resolved by this merge",
          one("SELECT count(*) FROM lead_identity_conflicts WHERE lead_id=%s AND resolved_by_merge=%s",
              (dup, m["id"])) >= 1
          and one("SELECT count(*) FROM lead_merge_queue WHERE other_lead_id=%s", (dup,)) == 0)
    check("lead_identity_claim of the dup's old URL on the keeper returns ours",
          one("SELECT lead_identity_claim(%s, 'linkedin_url', %s, 'fixture', false)",
              (keeper, IN + "merge-fixture-dup")) == "ours")

    # ── after: writes aimed at the merged lead are refused ────────────────────
    check("refuses merging an already-merged row",
          raises("SELECT merge_lead(%s, %s, 'fixture')", (dup, keeper)) == BAD_ARG)
    check("an insert aimed at the merged lead is refused",
          raises("INSERT INTO lead_tags (lead_id, tag) VALUES (%s, 'fixture:late')", (dup,)) == REFUSED)
    check("a multi-row insert with one merged lead is refused",
          raises("INSERT INTO lead_sources (lead_id, source_type, source_label) VALUES "
                 "(%s, 'manual_add', 'a'), (%s, 'manual_add', 'b')", (keeper, dup)) == REFUSED)
    check("an update re-pointing a row at the merged lead is refused",
          raises("UPDATE lead_meetings SET lead_id=%s WHERE lead_id=%s", (dup, keeper)) == REFUSED)
    check("writes to the keeper still work",
          raises("INSERT INTO lead_tags (lead_id, tag) VALUES (%s, 'fixture:late')", (keeper,)) is None)
    check("an update that leaves a stayed row's lead alone is allowed",
          raises("UPDATE campaign_members SET notes='touched' WHERE lead_id=%s", (dup,)) is None)

    # ── a group of three, keeper chosen against the identity owner ────────────
    urn3 = "ACoAA" + "MERGE" * 7 + "03"
    owner = lead("merge-fixture-three-owner", urn3)
    chosen = lead("merge-fixture-three-chosen", urn3)
    third = lead("merge-fixture-three-third", urn3)
    check("a group of three queues two pairs against the owner",
          one("SELECT count(*) FROM lead_merge_queue WHERE value=%s AND owner_lead_id=%s", (urn3, owner)) == 2)
    check("the owner merges into a keeper that holds the conflict",
          raises("SELECT merge_lead(%s, %s, 'fixture')", (owner, chosen)) is None)
    check("the third row's finding now names the keeper",
          one("SELECT count(*) FROM lead_merge_queue WHERE value=%s AND owner_lead_id=%s AND other_lead_id=%s",
              (urn3, chosen, third)) == 1
          and one("SELECT count(*) FROM lead_identity_conflicts WHERE lead_id=%s AND merged_from_owner_lead_id=%s",
                  (third, owner)) == 1)
    check("the keeper owns the URN identity now",
          one("SELECT lead_id FROM lead_identities WHERE kind='linkedin_urn' AND value=%s", (urn3,)) == chosen)
    check("the third row then merges into the same keeper",
          raises("SELECT merge_lead(%s, %s, 'fixture')", (third, chosen)) is None
          and one("SELECT count(*) FROM lead_merge_queue WHERE value=%s", (urn3,)) == 0)

    # ── a keeper keyed on its URN-form URL takes the dup's vanity URL (081) ───
    urn4 = "ACoAA" + "MERGE" * 7 + "04"
    cur.execute("INSERT INTO leads (linkedin_url, name) VALUES (%s, 'Merge Fixture urn-keyed') RETURNING id",
                (IN + urn4,))
    urn_keeper = cur.fetchone()[0]
    cur.execute("SELECT lead_identity_claim(%s, 'linkedin_urn', %s, 'check_merge_lead')", (urn_keeper, urn4))
    vanity_dup = lead("merge-fixture-vanity", urn4)
    cur.execute("SELECT merge_lead(%s, %s, 'check_merge_lead')", (vanity_dup, urn_keeper))
    m4 = cur.fetchone()[0]
    m4 = m4 if isinstance(m4, dict) else json.loads(m4)
    cur.execute("SELECT linkedin_url, public_id FROM leads WHERE id=%s", (urn_keeper,))
    k4 = cur.fetchone()
    check("a URN-form keeper takes the dup's vanity URL, and the old URL is in lead_merges.replaced",
          k4 == (IN + "merge-fixture-vanity", "merge-fixture-vanity")
          and m4["replaced"].get("linkedin_url") == IN + urn4, (k4, m4.get("replaced")))
    check("the vanity URL is the keeper's primary identity",
          one("SELECT count(*) FROM lead_identities WHERE lead_id=%s AND kind='linkedin_url' AND value=%s "
              "AND is_primary", (urn_keeper, IN + "merge-fixture-vanity")) == 1)
    check("a keeper with its own vanity URL keeps it",
          one("SELECT linkedin_url FROM leads WHERE id=%s", (keeper,)) == IN + "merge-fixture-keeper")

    conn.rollback()
    return total, fails


def check_dedupe_refuses() -> tuple[int, list[str]]:
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "dedupe_leads.py"), "--client", "anything"],
                       capture_output=True, text=True)
    ok = r.returncode != 0 and "merge_lead" in (r.stdout + r.stderr)
    print(f"  {'ok  ' if ok else 'FAIL'} tools/dedupe_leads.py refuses and points at merge_lead")
    return 1, [] if ok else ["dedupe_leads refuses"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url")
    ap.add_argument("--instance")
    args = ap.parse_args()
    conn = _connect(args)
    try:
        total, fails = run(conn)
    finally:
        conn.rollback()
        conn.close()
    t2, f2 = check_dedupe_refuses()
    total += t2
    fails += f2
    print(f"\n{total - len(fails)}/{total} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
