#!/usr/bin/env python3
"""Exercise the company-relationship door (schema/102) against a database that
already has every migration applied. Everything runs inside ONE transaction
that is always rolled back: no row survives, on any instance.

What it proves, step by step on synthetic companies:
  * recording a competitor creates one link, a live marker and a scope, and the
    old company_relationships row follows (transition mirroring);
  * an identical write changes nothing; a changed one closes the old claim with
    "superseded by <id>" and opens a new one, so history keeps every state;
  * monitor, client choice and vetting are separate decisions; a client choice
    must cite its artifact; decisions on a link that is not live are refused;
  * stopping monitoring keeps the company on the left-out list;
  * a possible competitor is the same link with confidence 'suspected', and the
    old row's relationship changes in place;
  * withdrawing a mirrored link is refused while the old table is a table;
    withdrawing any other link closes every live claim and deletes nothing;
  * the new format in the old table's shape equals the old table.

Usage:
  python3 scripts/check_company_relationship_door.py                 # MARKETBASE_URL (CI)
  python3 scripts/check_company_relationship_door.py --url postgresql://...
  python3 scripts/check_company_relationship_door.py --instance <name>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

CHECK_VIOLATION = "23514"
WRITER = "check_company_relationship_door"
OPERATOR = "the operator (check)"

LEGACY_COLS = ("company_id", "relationship", "scope", "status", "monitor_desired",
               "client_disposition", "client_disposition_source", "rep_discovery")


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

    def attempt(sql, params=()):
        cur.execute("SAVEPOINT k")
        try:
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
            cur.execute("RELEASE SAVEPOINT k")
            return None, row, None
        except Exception as e:  # noqa: BLE001 - the error IS the result
            cur.execute("ROLLBACK TO SAVEPOINT k")
            return _sqlstate(e), None, str(e).splitlines()[0]

    def refused(name, sql, params=(), needle=None):
        state, _, msg = attempt(sql, params)
        ok = state == CHECK_VIOLATION and (needle is None or (msg and needle in msg))
        check(name, ok, f"got sqlstate {state}: {msg}")

    def one(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchone()

    def counts():
        return one("SELECT (SELECT count(*) FROM assertions), (SELECT count(*) FROM company_edges), "
                   "(SELECT count(*) FROM company_relationships)")

    def state(link):
        cur.execute("SELECT relationship, possible, scope, status, monitor, client_choice, client_choice_source, "
                    "leave_out FROM company_link_state WHERE link_id = %s", (link,))
        r = cur.fetchone()
        return None if r is None else dict(zip(("relationship", "possible", "scope", "status", "monitor",
                                                 "client_choice", "client_choice_source", "leave_out"), r))

    def legacy(company):
        cur.execute(f"SELECT {', '.join(LEGACY_COLS)}, id FROM company_relationships WHERE company_id = %s",
                    (company,))
        rows = cur.fetchall()
        return [dict(zip(LEGACY_COLS + ("id",), r)) for r in rows]

    def legacy_equal(label):
        cols = ", ".join(LEGACY_COLS)
        cur.execute(f"""SELECT count(*) FROM (
                          (SELECT {cols} FROM company_relationships
                            WHERE company_id IN (SELECT from_company_id FROM company_edges WHERE to_company_id = %(own)s)
                           EXCEPT SELECT {cols} FROM v_company_relationships_from_links)
                          UNION ALL
                          (SELECT {cols} FROM v_company_relationships_from_links WHERE link_id IS NOT NULL
                           EXCEPT SELECT {cols} FROM company_relationships)) d""", {"own": own})
        n = cur.fetchone()[0]
        check(f"old table equals the new format in its shape ({label})", n == 0, f"{n} differing rows")

    def company(slug, name):
        cur.execute("INSERT INTO companies (linkedin_slug, name) VALUES (%s, %s) RETURNING id", (slug, name))
        return cur.fetchone()[0]

    # ── fixtures ──
    own = company("zz-door-acme", "ZZ Acme")
    brand = company("zz-door-acme-brand", "ZZ Acme Brand")
    rival = company("zz-door-rival", "ZZ Rival")
    parent = company("zz-door-parent", "ZZ Parent Holdings")
    other_client = company("zz-door-other-client", "ZZ Other Client")
    before = counts()

    # ── no own company recorded yet ──
    cur.execute("SELECT own_company()")
    check("own_company() is NULL before one is recorded", cur.fetchone()[0] is None)
    cur.execute("INSERT INTO company_relationships (company_id, relationship, source) VALUES (%s, 'competitor', 'x')",
                (parent,))
    refused("a door write is refused while the old table holds the company and no own company is recorded",
            "SELECT set_company_relationship(%s, %s, 'competitor', %s, 'n', 'direct')",
            (parent, own, WRITER), needle="no own company recorded")
    cur.execute("DELETE FROM company_relationships WHERE company_id = %s", (parent,))  # fixture only, rolled back

    for co, v in ((own, "primary"), (brand, "brand")):
        cur.execute("INSERT INTO assertions (subject_company_id, namespace, key, value, asserted_by) "
                    "VALUES (%s, 'client', 'own_company', %s, %s)", (co, v, WRITER))
    cur.execute("SELECT own_company()")
    check("own_company() returns the primary own company", cur.fetchone()[0] == own)

    # ── refusals before anything is written ──
    refused("a company linked to itself is refused",
            "SELECT company_link(%s, %s, 'competitor')", (own, own), needle="itself")
    refused("an unregistered relationship is refused",
            "SELECT company_link(%s, %s, 'frenemy')", (rival, own), needle="not registered")
    refused("a fact is not a kind of link",
            "SELECT company_link(%s, %s, 'scope')", (rival, own), needle="fact about a link")
    refused("a bad scope is refused",
            "SELECT set_company_relationship(%s, %s, 'competitor', %s, 'n', 'close')", (rival, own, WRITER),
            needle="scope must be")
    refused("only a competitor can be possible",
            "SELECT set_company_relationship(%s, %s, 'other', %s, 'n', NULL, true)", (parent, own, WRITER),
            needle="only a competitor")

    # ── record a competitor ──
    cur.execute("SELECT set_company_relationship(%s, %s, 'competitor', %s, %s, 'direct', false, %s, 'manual')",
                (rival, own, WRITER, "sells against Acme on every deal", OPERATOR))
    link = cur.fetchone()[0]
    s = state(link)
    check("recording a competitor makes a live link with scope direct",
          s and s["relationship"] == "competitor" and s["scope"] == "direct" and not s["possible"], s)
    lg = legacy(rival)
    check("the old table gets one row, competitor / direct",
          len(lg) == 1 and lg[0]["relationship"] == "competitor" and lg[0]["scope"] == "direct", lg)
    legacy_id = lg[0]["id"] if lg else None
    marker_kind = one("SELECT kind, decided_by, via FROM assertions WHERE subject_company_edge_id = %s "
                      "AND namespace = 'relationship' AND key = 'competitor' AND removed_at IS NULL", (link,))
    check("a marker written with a decider is a decision", marker_kind == ("decision", OPERATOR, "manual"),
          marker_kind)
    legacy_equal("after recording")

    n0 = counts()[0]
    cur.execute("SELECT set_company_relationship(%s, %s, 'competitor', %s, %s, 'direct', false, %s, 'manual')",
                (rival, own, WRITER, "sells against Acme on every deal", OPERATOR))
    check("the same write again returns the same link", cur.fetchone()[0] == link)
    check("the same write again adds no assertion", counts()[0] == n0, (n0, counts()[0]))

    # ── change the scope: history keeps both ──
    cur.execute("SELECT set_company_relationship(%s, %s, 'competitor', %s, %s, 'adjacent', false, %s, 'manual')",
                (rival, own, WRITER, "sells against Acme on every deal", OPERATOR))
    s = state(link)
    check("scope changes to adjacent", s["scope"] == "adjacent", s)
    hist = one("SELECT count(*), count(*) FILTER (WHERE removed_reason LIKE 'superseded by %%') "
               "FROM company_link_history WHERE link_id = %s AND key = 'scope'", (link,))
    check("history holds both scopes, the old one 'superseded by <id>'", hist == (2, 1), hist)
    check("the old row's scope follows", legacy(rival)[0]["scope"] == "adjacent")

    # ── decisions ──
    refused("a monitor decision with no decider is refused",
            "SELECT set_link_decision(%s, 'monitor', 'monitor', NULL, 'manual', 'r', %s)", (link, WRITER),
            needle="needs decided_by")
    refused("a decision with an unknown via is refused",
            "SELECT set_link_decision(%s, 'monitor', 'monitor', %s, 'gut', 'r', %s)", (link, OPERATOR, WRITER),
            needle="via")
    refused("a decision with no reason is refused",
            "SELECT set_link_decision(%s, 'monitor', 'monitor', %s, 'manual', '', %s)", (link, OPERATOR, WRITER),
            needle="reason")
    refused("an unknown monitor value is refused",
            "SELECT set_link_decision(%s, 'monitor', 'maybe', %s, 'manual', 'r', %s)", (link, OPERATOR, WRITER))
    refused("an unknown fact is refused",
            "SELECT set_link_decision(%s, 'mood', 'x', %s, 'manual', 'r', %s)", (link, OPERATOR, WRITER))
    cur.execute("SELECT set_link_decision(%s, 'monitor', 'monitor', %s, 'manual', %s, %s)",
                (link, OPERATOR, "the client asked for its sellers", WRITER))
    check("a monitor decision lands", state(link)["monitor"] == "monitor")
    check("the old row's monitor_desired follows", legacy(rival)[0]["monitor_desired"] is True)
    cur.execute("SELECT set_link_decision(%s, 'client_choice', 'selected', 'Acme (the client)', 'manual', %s, %s)",
                (link, "selection workbook 2026-01-01, row 4", WRITER))
    s = state(link)
    check("a client choice lands with its citation",
          s["client_choice"] == "selected" and s["client_choice_source"].startswith("selection workbook"), s)
    lg = legacy(rival)[0]
    check("the old row's client disposition follows with its source",
          lg["client_disposition"] == "selected" and lg["client_disposition_source"].startswith("selection workbook"),
          lg)
    cur.execute("SELECT set_link_decision(%s, 'status', 'clear', %s, 'manual', 'no conflict', %s)",
                (link, OPERATOR, WRITER))
    check("vetting lands and the old row follows", legacy(rival)[0]["status"] == "clear")
    legacy_equal("after decisions")

    # ── stop monitoring keeps it on the left-out list ──
    check("not on the left-out list before stopping", state(link)["leave_out"] is False)
    cur.execute("SELECT stop_monitoring(%s, %s, 'manual', %s, %s)",
                (link, OPERATOR, "the client stopped asking for it", WRITER))
    s = state(link)
    check("stopping monitoring: do_not_monitor and on the left-out list",
          s["monitor"] == "do_not_monitor" and s["leave_out"] is True, s)
    lo = one("SELECT notes, decided_by FROM assertions WHERE subject_company_edge_id = %s AND namespace = 'scan' "
             "AND key = 'leave_out' AND removed_at IS NULL", (link,))
    check("the leave-out decision says why and who",
          lo and lo[0].startswith("kept on the left-out list when monitoring stopped") and lo[1] == OPERATOR, lo)
    check("the old row's monitor_desired is false", legacy(rival)[0]["monitor_desired"] is False)

    # ── possible competitor: same link, confidence suspected, old row in place ──
    cur.execute("SELECT set_company_relationship(%s, %s, 'competitor', %s, %s, NULL, true, %s, 'manual')",
                (rival, own, WRITER, "not sure it competes after all", OPERATOR))
    check("marking it possible keeps the same link", cur.fetchone()[0] == link)
    s = state(link)
    check("the link is possible and keeps its decisions",
          s["possible"] is True and s["client_choice"] == "selected" and s["leave_out"] is True, s)
    lg = legacy(rival)
    check("the old row turns suspected_competitor in place (same row id)",
          len(lg) == 1 and lg[0]["relationship"] == "suspected_competitor" and lg[0]["id"] == legacy_id, lg)
    legacy_equal("after marking possible")

    # ── withdraw ──
    refused("withdrawing a link the old table mirrors is refused during the transition",
            "SELECT withdraw_company_link(%s, %s, 'typo', %s)", (link, OPERATOR, WRITER), needle="ticket 04")
    cur.execute("SELECT set_company_relationship(%s, %s, 'other', %s, %s, NULL, false, %s, 'manual')",
                (parent, other_client, WRITER, "parent of a monitored competitor", OPERATOR))
    plink = cur.fetchone()[0]
    cur.execute("SELECT set_link_decision(%s, 'leave_out', 'leave_out', %s, 'manual', 'ticked', %s)",
                (plink, OPERATOR, WRITER))
    check("a link to a company that is not the own company leaves the old table alone", legacy(parent) == [])
    edges_before = counts()[1]
    cur.execute("SELECT withdraw_company_link(%s, %s, 'wrong parent', %s)", (plink, OPERATOR, WRITER))
    closed = cur.fetchone()[0]
    check("withdrawing closes every live claim on the link (marker and leave-out)", closed == 2, closed)
    check("a withdrawn link is not live", state(plink) is None)
    reasons = one("SELECT count(*) FROM assertions WHERE subject_company_edge_id = %s "
                  "AND removed_reason LIKE 'added by mistake: wrong parent (decided by %%'", (plink,))
    check("each closed claim says added by mistake, why and who", reasons == (2,), reasons)
    check("the link row stays", counts()[1] == edges_before)
    refused("a decision on a withdrawn link is refused",
            "SELECT set_link_decision(%s, 'monitor', 'monitor', %s, 'manual', 'r', %s)", (plink, OPERATOR, WRITER),
            needle="not live")
    cur.execute("SELECT set_company_relationship(%s, %s, 'other', %s, %s, NULL, false, %s, 'manual')",
                (parent, other_client, WRITER, "it was the parent after all", OPERATOR))
    check("re-recording a withdrawn link revives the same link", cur.fetchone()[0] == plink and state(plink))

    # ── nothing deleted ──
    after = counts()
    check("assertions only grew", after[0] > before[0], (before, after))
    cur.execute("SELECT count(*) FROM company_link_history WHERE link_id IN (%s, %s)", (link, plink))
    check("history is readable for both links", cur.fetchone()[0] >= 12)
    print(json.dumps(state(link), default=str))
    return total, fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--instance")
    args = ap.parse_args()
    conn = _connect(args)
    try:
        total, fails = run(conn)
    finally:
        conn.rollback()
        conn.close()
    print(f"{total - len(fails)} of {total} passed; everything rolled back")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
