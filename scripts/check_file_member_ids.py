#!/usr/bin/env python3
"""Prove tools/file_member_ids.py's plan on a fixture, with no network: the lookups it reads
are seeded into enrichment_calls. Every write runs inside ONE transaction that is always
rolled back.

What it proves:
  * a dead handle (the lookup answered "not found" for the URL the other lead already holds)
    is settled: the claim returns duplicate:<holder>, the row carries the tag pair, and the
    conflict's source is marked DECIDED, so --url-conflicts never selects it again;
  * a pair whose two rows resolve to one member ID: the holder's ID lands (inserted) and the
    other row's claim records the same person twice, so the pair enters lead_merge_queue;
  * a lookup whose name does not match is skipped, and filed once --accept-name names the
    variant;
  * a "not found" with no conflict behind it writes nothing and is left for a person;
  * the plan's invariants hold, and a second plan over the same leads writes nothing.

Usage:
  python3 scripts/check_file_member_ids.py                 # MARKETBASE_URL (CI)
  python3 scripts/check_file_member_ids.py --url postgresql://...
  python3 scripts/check_file_member_ids.py --instance <name>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import file_member_ids as fmi  # noqa: E402

IN = "https://www.linkedin.com/in/"
X = "ACoAA" + "FMIFIXTURE" * 3 + "X1"
Y = "ACoAA" + "FMIFIXTURE" * 3 + "Y1"
Z = "ACoAA" + "FMIFIXTURE" * 3 + "Z1"
S1 = "ACwAA" + "FMISEARCH0" * 3 + "01"
S2 = "ACwAA" + "FMISEARCH0" * 3 + "02"
NOT_FOUND = {"data": None, "message": "The url was not found on Linkedin."}


def _connect(args):
    if args.instance:
        import lib
        return lib.connect(args.instance)
    url = args.url or os.environ.get("MARKETBASE_URL")
    if not url:
        sys.exit("No database. Pass --url, --instance, or set MARKETBASE_URL.")
    import psycopg
    return psycopg.connect(url, autocommit=False)


def run(conn) -> list[str]:
    cur = conn.cursor()
    fails: list[str] = []

    def check(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  ' + str(detail)}")
        if not cond:
            fails.append(name)

    def one(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchone()[0]

    def lead(handle, name):
        cur.execute("INSERT INTO leads (linkedin_url, name) VALUES (%s, %s) RETURNING id::text", (IN + handle, name))
        return cur.fetchone()[0]

    def answer(handle, resp, ok):
        cur.execute("""INSERT INTO enrichment_calls (api, endpoint, params, success, response, fetched_at)
                       VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, now())""",
                    (fmi.PROVIDER, fmi.ENDPOINT, json.dumps({"linkedin_url": IN + handle}), ok, json.dumps(resp)))

    def found(urn, full_name):
        return {"data": {"urn": urn, "full_name": full_name}, "message": "ok"}

    # ── fixture ──────────────────────────────────────────────────────────────
    # dead handle: the holder's new URL resolves, its old URL (now the dead row's link) does not
    holder = lead("fmi-dana-new", "Dana Example")
    cur.execute("SELECT lead_identity_claim(%s, 'linkedin_url', %s, 'check_file_member_ids', false)",
                (holder, IN + "fmi-dana-old"))
    dead = lead("fmi-dana-old", "Dana Example")
    answer("fmi-dana-new", found(X, "Dana Example"), True)
    answer("fmi-dana-old", NOT_FOUND, False)
    # a pair that resolves to one member ID
    p1 = lead("fmi-ari-new", "Ari Sample")
    cur.execute("SELECT lead_identity_claim(%s, 'linkedin_url', %s, 'check_file_member_ids', false)",
                (p1, IN + "fmi-ari-old"))
    p2 = lead("fmi-ari-old", "Ari Sample")
    answer("fmi-ari-new", found(Y, "Ari Sample"), True)
    answer("fmi-ari-old", found(Y, "Ari Sample"), True)
    # search form only: one nickname, one gone
    s1 = lead(S1, "Robert Fixture")
    s2 = lead(S2, "Lee Fixture")
    answer(S1, found(Z, "Bob Fixture"), True)
    answer(S2, NOT_FOUND, False)
    mine = {holder, dead, p1, p2, s1, s2}

    check("the fixture's two URL conflicts exist",
          one("SELECT count(*) FROM lead_identity_conflicts WHERE kind = 'linkedin_url' AND lead_id = ANY(%s::uuid[])",
              ([dead, p2],)) == 2)
    check("the search-form leads hold their ID as linkedin_urn_search",
          one("SELECT count(*) FROM lead_identities WHERE kind = 'linkedin_urn_search' AND lead_id = ANY(%s::uuid[])",
              ([s1, s2],)) == 2)

    def fixture_targets(mode):
        t, c = fmi.select_targets(cur, mode)
        return ({k: v for k, v in t.items() if k in mine},
                [x for x in c if x["lead"] in mine or x["other"] in mine])

    # ── URL conflicts ────────────────────────────────────────────────────────
    t, c = fixture_targets("url-conflicts")
    check("--url-conflicts selects the four conflict rows", set(t) == {holder, dead, p1, p2}, sorted(t))
    p = fmi.plan(cur, t, c, "fixture", {})
    check("plan invariants hold", not p["fails"], p["fails"])
    check("three member IDs and one dead handle planned", p["writes"] == 4, p["lines"])
    check("the holder's member ID landed",
          one("SELECT count(*) FROM lead_identities WHERE lead_id = %s AND kind = 'linkedin_urn' AND value = %s",
              (holder, X)) == 1)
    check("the dead row is tagged duplicate + do_not_push",
          one("SELECT count(*) FROM lead_tags WHERE lead_id = %s AND tag IN "
              "('flag:duplicate_lead', 'expansion:do_not_push')", (dead,)) == 2)
    check("the dead row's conflict is marked DECIDED by the approver",
          one("SELECT count(*) FROM lead_identity_conflicts WHERE lead_id = %s AND kind = 'linkedin_url' "
              "AND source LIKE '%%DECIDED%%by fixture%%'", (dead,)) == 1)
    check("the dead row got no member ID", one("SELECT count(*) FROM lead_identities WHERE lead_id = %s "
                                               "AND kind = 'linkedin_urn'", (dead,)) == 0)
    check("the pair's second row records the same person (conflict on the member ID)",
          one("SELECT count(*) FROM lead_identity_conflicts WHERE lead_id = %s AND kind = 'linkedin_urn' "
              "AND value = %s AND owner_lead_id = %s", (p2, Y, p1)) == 1)
    check("the pair is in lead_merge_queue",
          one("SELECT count(*) FROM lead_merge_queue WHERE owner_lead_id = %s AND other_lead_id = %s", (p1, p2)) == 1)
    t2, c2 = fixture_targets("url-conflicts")
    check("a second run selects nothing: decided conflicts and rows with an ID drop out", not t2, sorted(t2))

    # ── search form only ─────────────────────────────────────────────────────
    t, c = fixture_targets("search-id-only")
    check("--search-id-only selects the two search-form leads", set(t) == {s1, s2}, sorted(t))
    cur.execute("SAVEPOINT strict")
    p = fmi.plan(cur, t, c, "fixture", {})
    check("a name that does not match is skipped, and the gone lead writes nothing", p["writes"] == 0, p["lines"])
    check("the skip and the gone lead are both reported",
          any("skip" in r and "Robert" in r for r in p["report"])
          and any("gone" in r and "Lee" in r for r in p["report"]), p["report"])
    cur.execute("ROLLBACK TO SAVEPOINT strict")
    p = fmi.plan(cur, t, c, "fixture", {s1: "bob"})
    check("--accept-name files the variant", p["writes"] == 1 and not p["fails"], (p["lines"], p["fails"]))
    check("the search-form lead now has its member ID",
          one("SELECT count(*) FROM lead_identities WHERE lead_id = %s AND kind = 'linkedin_urn' AND value = %s",
              (s1, Z)) == 1)
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url")
    ap.add_argument("--instance")
    args = ap.parse_args()
    conn = _connect(args)
    try:
        fails = run(conn)
    finally:
        conn.rollback()
    print("\nALL OK (rolled back)" if not fails else f"\n{len(fails)} FAILED (rolled back)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
