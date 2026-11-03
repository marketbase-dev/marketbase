#!/usr/bin/env python3
"""Exercise the lead_identities sync trigger (schema/070) against a database
that already has every migration applied. Everything runs inside ONE
transaction that is always rolled back: no row survives, on any instance.

Why this exists: `trg_leads_sync_identities` is the mechanism that makes "a
lead creator forgot to write lead_identities" impossible. It is SQL, so none
of the Python checks see it. These checks were first run by hand against a
live instance on 2026-11-02 before 070 merged; this file keeps them, and CI
runs them against the fresh Postgres it has just migrated.

Usage:
  python3 scripts/check_identity_trigger.py                 # MARKETBASE_URL (CI)
  python3 scripts/check_identity_trigger.py --url postgresql://...
  python3 scripts/check_identity_trigger.py --instance <name>
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

TRIGGER = "trg_leads_sync_identities"
# Shaped like member URNs (39 chars, ACoAA profile / ACwAA search namespace),
# deliberately low entropy so a secret scanner has nothing to find.
URN_A = "ACoAA" + "TEST" * 8 + "01"
URN_B = "ACoAA" + "TEST" * 8 + "02"
URN_S = "ACwAA" + "TEST" * 8 + "03"
IN = "https://www.linkedin.com/in/"


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

    def idents(lid):
        cur.execute("SELECT kind, value, is_primary, source, verified_at FROM lead_identities "
                    "WHERE lead_id=%s ORDER BY kind, created_at", (lid,))
        return cur.fetchall()

    def tags(lid):
        cur.execute("SELECT tag, notes, tagged_by FROM lead_tags WHERE lead_id=%s ORDER BY tag", (lid,))
        return cur.fetchall()

    cur.execute("SELECT count(*) FROM pg_trigger WHERE tgname=%s AND NOT tgisinternal", (TRIGGER,))
    check("the trigger is installed", cur.fetchone()[0] == 1)
    cur.execute("SELECT count(*) FROM lead_identity_kinds WHERE name='linkedin_urn_search'")
    check("alias kind linkedin_urn_search is seeded", cur.fetchone()[0] == 1)
    cur.execute("SELECT count(*) FROM tag_definitions WHERE (namespace,key) IN "
                "(('flag','duplicate_lead'),('expansion','do_not_push'))")
    check("tag_definitions holds both duplicate tags", cur.fetchone()[0] == 2)

    # A: vanity URL + canonical URN -> two identity rows
    cur.execute("INSERT INTO leads (linkedin_url, member_urn, name) VALUES (%s,%s,%s) RETURNING id",
                (IN + "zz-test-vanity-a", URN_A, "Test A"))
    a = cur.fetchone()[0]
    ia = idents(a)
    check("A: two identity rows", len(ia) == 2, ia)
    check("A: vanity URL filed as linkedin_url, primary",
          any(k == "linkedin_url" and v.endswith("/in/zz-test-vanity-a") and p for k, v, p, *_ in ia), ia)
    check("A: URN filed as linkedin_urn, primary, source=trigger, unverified",
          any(k == "linkedin_urn" and v == URN_A and p and s == TRIGGER and ver is None
              for k, v, p, s, ver in ia), ia)

    # B: URN-form URL only -> URN identity only, no vanity row
    cur.execute("INSERT INTO leads (linkedin_url, name) VALUES (%s,%s) RETURNING id",
                (IN + URN_B, "Test B"))
    b = cur.fetchone()[0]
    ib = idents(b)
    check("B: a URN-form URL yields one linkedin_urn row and no vanity row",
          len(ib) == 1 and ib[0][0] == "linkedin_urn" and ib[0][1] == URN_B, ib)

    # C: handle change on A -> old vanity demoted to alias, new one primary
    cur.execute("UPDATE leads SET linkedin_url=%s WHERE id=%s", (IN + "zz-test-vanity-a2", a))
    ia2 = idents(a)
    urls = {v: p for k, v, p, *_ in ia2 if k == "linkedin_url"}
    check("C: two vanity rows, old demoted, new primary",
          len(urls) == 2 and urls.get(IN + "zz-test-vanity-a") is False
          and urls.get(IN + "zz-test-vanity-a2") is True, ia2)
    check("C: the URN row is untouched", sum(1 for k, *_ in ia2 if k == "linkedin_urn") == 1)

    # D: an update that changes neither column adds nothing
    cur.execute("UPDATE leads SET updated_at=now() WHERE id=%s", (a,))
    check("D: a no-op update adds nothing", idents(a) == ia2)

    # E: a new lead under A's OLD handle -> the same person: tagged, never reassigned.
    # This is the duplicate path every instance can reach: leads.linkedin_url is
    # unique, so a duplicate arrives through an alias the owner no longer uses.
    cur.execute("INSERT INTO leads (linkedin_url, name) VALUES (%s,%s) RETURNING id",
                (IN + "zz-test-vanity-a", "Test E"))
    e = cur.fetchone()[0]
    te = tags(e)
    cur.execute("SELECT lead_id, is_primary FROM lead_identities WHERE kind='linkedin_url' AND value=%s",
                (IN + "zz-test-vanity-a",))
    check("E: the old handle is still A's identity, still an alias", cur.fetchone() == (a, False))
    check("E: the duplicate gets no identity row of its own", idents(e) == [], idents(e))
    check("E: both duplicate tags written by the trigger",
          [t for t, *_ in te] == ["expansion:do_not_push", "flag:duplicate_lead"]
          and all(tb == TRIGGER for *_, tb in te), te)
    check("E: the note names the owner and its current URL",
          all(str(a) in n and "zz-test-vanity-a2" in n for _, n, _ in te), te)
    check("E: A is untouched", idents(a) == ia2)

    # E2: a new lead carrying A's member URN. Instances that dedupe_leads.py has
    # already deduped carry the partial-unique index uq_leads_member_urn (027),
    # which rejects this at the column before any trigger runs, so there the
    # case is reported as skipped rather than asserted.
    cur.execute("SELECT count(*) FROM pg_indexes WHERE tablename='leads' AND indexname='uq_leads_member_urn'")
    if cur.fetchone()[0]:
        print("  skip E2: uq_leads_member_urn enforces member_urn uniqueness at the column here")
    else:
        cur.execute("INSERT INTO leads (linkedin_url, member_urn, name) VALUES (%s,%s,%s) RETURNING id",
                    (IN + "zz-test-vanity-e2", URN_A, "Test E2"))
        e2 = cur.fetchone()[0]
        ie2, te2 = idents(e2), tags(e2)
        cur.execute("SELECT lead_id FROM lead_identities WHERE kind='linkedin_urn' AND value=%s", (URN_A,))
        check("E2: the URN identity is still owned by A", cur.fetchone()[0] == a)
        check("E2: the duplicate got its own vanity row only",
              len(ie2) == 1 and ie2[0][0] == "linkedin_url", ie2)
        check("E2: both duplicate tags written by the trigger",
              [t for t, *_ in te2] == ["expansion:do_not_push", "flag:duplicate_lead"]
              and all(tb == TRIGGER for *_, tb in te2), te2)
        check("E2: A is untouched", idents(a) == ia2)

    # F: search-namespace token -> alias kind
    cur.execute("INSERT INTO leads (linkedin_url, member_urn, name) VALUES (%s,%s,%s) RETURNING id",
                (IN + "zz-test-vanity-f", URN_S, "Test F"))
    f = cur.fetchone()[0]
    i_f = idents(f)
    check("F: an ACwAA token is filed as linkedin_urn_search",
          any(k == "linkedin_urn_search" and v == URN_S for k, v, *_ in i_f), i_f)

    # G: an upsert ON CONFLICT (linkedin_url) DO UPDATE on an existing row adds nothing
    cur.execute("""INSERT INTO leads (linkedin_url, name) VALUES (%s,%s)
                   ON CONFLICT (linkedin_url) DO UPDATE SET name=EXCLUDED.name, updated_at=now()""",
                (IN + "zz-test-vanity-a2", "Test A renamed"))
    check("G: a conflict-update adds no identity rows", idents(a) == ia2)

    # H: claiming a value this lead already holds as a non-primary row promotes it
    cur.execute("INSERT INTO lead_identities (lead_id, kind, value, is_primary, source) "
                "VALUES (%s,'linkedin_url',%s,false,'check_identity_trigger')",
                (b, IN + "zz-test-vanity-b"))
    cur.execute("UPDATE leads SET linkedin_url=%s WHERE id=%s", (IN + "zz-test-vanity-b", b))
    ib2 = idents(b)
    check("H: a pre-existing own row is promoted to primary, no duplicate row",
          [(k, p) for k, v, p, *_ in ib2 if k == "linkedin_url"] == [("linkedin_url", True)], ib2)

    # I: the claim function as the TOOLS call it (071): an alias is recorded
    # non-primary without demoting, a verification fills a NULL verified_at and
    # never overwrites one, and a value owned elsewhere returns the owner.
    def claim(lid, kind, value, primary, verified_at):
        cur.execute("SELECT lead_identity_claim(%s, %s, %s, 'check_identity_trigger', %s, %s)",
                    (lid, kind, value, primary, verified_at))
        return cur.fetchone()[0]

    alias = IN + "zz-test-vanity-f-alias"
    check("I: an alias claim is inserted", claim(f, "linkedin_url", alias, False, None) == "inserted")
    i_f2 = {v: (p, ver) for k, v, p, s, ver in idents(f) if k == "linkedin_url"}
    check("I: the alias is non-primary and the row's own URL stays primary",
          i_f2.get(alias) == (False, None) and i_f2.get(IN + "zz-test-vanity-f", (None,))[0] is True, i_f2)
    check("I: claiming it again is 'ours' and adds nothing",
          claim(f, "linkedin_url", alias, False, None) == "ours" and len(idents(f)) == len(i_f2) + 1)
    cur.execute("SELECT now() - interval '1 day'")
    t1 = cur.fetchone()[0]
    claim(f, "linkedin_url", alias, False, t1)
    cur.execute("SELECT verified_at FROM lead_identities WHERE lead_id=%s AND kind='linkedin_url' AND value=%s",
                (f, alias))
    check("I: a verification fills a NULL verified_at", cur.fetchone()[0] == t1)
    cur.execute("SELECT now()")
    t2 = cur.fetchone()[0]
    claim(f, "linkedin_url", alias, False, t2)
    cur.execute("SELECT verified_at FROM lead_identities WHERE lead_id=%s AND kind='linkedin_url' AND value=%s",
                (f, alias))
    check("I: a later verification never overwrites an earlier one", cur.fetchone()[0] == t1)
    check("I: an empty value is 'empty'", claim(f, "linkedin_url", "", False, None) == "empty")

    cur.execute("SELECT lead_id, is_primary, source, verified_at FROM lead_identities "
                "WHERE kind='linkedin_urn' AND value=%s", (URN_B,))
    owner_row_before = cur.fetchone()
    out = claim(f, "linkedin_urn", URN_B, False, t2)
    check("I: a value owned elsewhere returns the owner", out == f"duplicate:{b}", out)
    cur.execute("SELECT lead_id, is_primary, source, verified_at FROM lead_identities "
                "WHERE kind='linkedin_urn' AND value=%s", (URN_B,))
    check("I: the owner's row is untouched, not even its verified_at", cur.fetchone() == owner_row_before)
    tf = tags(f)
    check("I: the duplicate is recorded as the two tags, by the tool's name",
          [t for t, *_ in tf] == ["expansion:do_not_push", "flag:duplicate_lead"]
          and all(tb == "check_identity_trigger" and str(b) in n for _, n, tb in tf), tf)
    return total, fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="Postgres URL. Overrides MARKETBASE_URL.")
    ap.add_argument("--instance", help="Named MarketBase instance (read, then rolled back).")
    args = ap.parse_args()
    conn = _connect(args)
    try:
        total, fails = run(conn)
    finally:
        conn.rollback()
        conn.close()
        print("rolled back: nothing was written")
    if fails:
        print(f"check_identity_trigger: {len(fails)} of {total} FAILED: {fails}")
        return 1
    print(f"check_identity_trigger: {total} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
