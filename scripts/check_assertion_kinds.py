#!/usr/bin/env python3
"""Exercise the observation / decision rules on `assertions` (schema/072)
against a database that already has every migration applied. Everything runs
inside ONE transaction that is always rolled back: no row survives, on any
instance.

Why this exists: the rules are CHECK constraints and a trigger, so a writer
that skips them is refused by the database, not by convention. None of the
Python checks can see SQL; this file proves the refusals happen, and that
unclassified rows (every writer that predates 072) are still accepted.

Usage:
  python3 scripts/check_assertion_kinds.py                 # MARKETBASE_URL (CI)
  python3 scripts/check_assertion_kinds.py --url postgresql://...
  python3 scripts/check_assertion_kinds.py --instance <name>
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"


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

    def attempt(sql, params):
        """Run one statement under a savepoint. Returns (sqlstate or None, row)."""
        cur.execute("SAVEPOINT k")
        try:
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
            cur.execute("RELEASE SAVEPOINT k")
            return None, row
        except Exception as e:  # noqa: BLE001 - the error IS the result
            cur.execute("ROLLBACK TO SAVEPOINT k")
            return _sqlstate(e), None

    def refused(name, sql, params, code=CHECK_VIOLATION):
        state, _ = attempt(sql, params)
        check(name, state == code, f"got sqlstate {state}")

    def accepted(name, sql, params):
        state, row = attempt(sql, params)
        check(name, state is None, f"got sqlstate {state}")
        return row[0] if row else None

    ins = ("INSERT INTO assertions (subject_company_id, subject_meeting_id, subject_lead_id, "
           "namespace, key, value, notes, kind, via, decided_by, basis, processor_id, "
           "evidence_signal_id, evidence_enrichment_call_id, asserted_by) "
           "VALUES (%(co)s, %(mt)s, %(ld)s, %(ns)s, %(key)s, %(val)s, %(notes)s, %(kind)s, "
           "%(via)s, %(by)s, %(basis)s, %(proc)s, %(sig)s, %(call)s, 'check_assertion_kinds') "
           "RETURNING id")

    def row(**kw):
        base = dict(co=None, mt=None, ld=None, ns="zz_test", key="k", val="v", notes=None,
                    kind=None, via=None, by=None, basis=None, proc=None, sig=None, call=None)
        base.update(kw)
        return base

    # ── fixtures ──
    cur.execute("INSERT INTO companies (linkedin_slug, name) VALUES ('zz-test-co', 'ZZ Test Co') RETURNING id")
    co = cur.fetchone()[0]
    cur.execute("INSERT INTO companies (linkedin_slug, name) VALUES ('zz-test-co-2', 'ZZ Test Co 2') RETURNING id")
    co2 = cur.fetchone()[0]
    cur.execute("INSERT INTO company_relationships (company_id, relationship) VALUES (%s, 'competitor')", (co,))
    cur.execute("INSERT INTO leads (name) VALUES ('ZZ Test Lead') RETURNING id")
    ld = cur.fetchone()[0]
    cur.execute("INSERT INTO lead_meetings (lead_id, scheduled_at) VALUES (%s, now()) RETURNING id", (ld,))
    mt = cur.fetchone()[0]
    cur.execute("INSERT INTO enrichment_calls (api, endpoint, success) VALUES ('zz_test', '/x', true) RETURNING id")
    call = cur.fetchone()[0]
    cur.execute("INSERT INTO signals (subject_company_id, enricher_name, payload) "
                "VALUES (%s, 'zz_test', '{}'::jsonb) RETURNING id", (co,))
    sig = cur.fetchone()[0]
    cur.execute("INSERT INTO processors (name, version, processor_type, yaml_spec) "
                "VALUES ('zz-test-rule', 't1', 'classifier', 'name: zz-test-rule') RETURNING id")
    proc = cur.fetchone()[0]

    # ── registry ──
    cur.execute("SELECT key, kind FROM tag_definitions WHERE entity_type='company' "
                "AND namespace IN ('client_evidence','monitoring') ORDER BY key")
    reg = dict(cur.fetchall())
    check("tag_definitions registers the four client_evidence observation keys",
          all(reg.get(k) == "observation" for k in
              ("deals_lost", "selection_sheet_priority", "engagement_roster", "g2_signals")), reg)
    check("tag_definitions registers monitoring/monitor as a decision", reg.get("monitor") == "decision", reg)

    # ── unclassified rows: every pre-072 writer keeps working ──
    accepted("an unclassified row (no kind) is still accepted", ins, row(co=co, key="legacy"))
    refused("an unclassified row carrying decided_by is refused", ins,
            row(co=co, key="legacy2", by="someone"))
    refused("an unknown kind is refused", ins, row(co=co, key="odd", kind="opinion"))

    # ── observations ──
    refused("an observation without evidence is refused", ins,
            row(co=co, key="o0", kind="observation"))
    obs_call = accepted("an observation citing an enrichment call is accepted", ins,
                        row(co=co, key="o1", kind="observation", call=call))
    obs_sig = accepted("an observation citing a signal is accepted", ins,
                       row(co=co, key="o2", kind="observation", sig=sig))
    refused("an observation naming a decider is refused", ins,
            row(co=co, key="o3", kind="observation", call=call, by="someone", via="manual"))

    # ── decisions ──
    refused("a decision without a decider is refused", ins,
            row(co=co, key="d0", kind="decision", via="manual"))
    refused("a decision with a blank decider is refused", ins,
            row(co=co, key="d1", kind="decision", via="manual", by="  "))
    refused("a decision without via is refused", ins,
            row(co=co, key="d2", kind="decision", by="someone"))
    refused("an unknown via is refused", ins,
            row(co=co, key="d3", kind="decision", by="someone", via="vibes"))
    refused("an automatic decision without a processor is refused", ins,
            row(co=co, key="d4", kind="decision", by="zz-test-rule@t1", via="automatic"))
    accepted("an automatic decision with a processor is accepted", ins,
             row(co=co, key="d5", kind="decision", by="zz-test-rule@t1", via="automatic", proc=proc))
    refused("a decision citing raw evidence is refused", ins,
            row(co=co, key="d6", kind="decision", by="someone", via="manual", call=call))
    refused("a decision whose basis is not an observation is refused", ins,
            row(co=co, key="d7", kind="decision", by="someone", via="manual", basis=[uuid.uuid4()]))
    refused("a decision with an empty basis is refused", ins,
            row(co=co, key="d8", kind="decision", by="someone", via="manual", basis=[]))
    dec = accepted("a decision resting on two observations is accepted", ins,
                   row(co=co, ns="monitoring", key="monitor", val="do_not_monitor",
                       notes="zz test reason", kind="decision", by="someone", via="manual",
                       basis=[obs_call, obs_sig]))
    refused("a second active decision on the same claim collides", ins,
            row(co=co, ns="monitoring", key="monitor", val="monitor", kind="decision",
                by="someone", via="manual"), code=UNIQUE_VIOLATION)

    # ── the meeting subject ──
    accepted("a decision about a meeting is accepted", ins,
             row(mt=mt, ns="zz_test", key="category", kind="decision", by="someone", via="manual"))
    refused("a row with two subjects is refused", ins,
            row(mt=mt, ld=ld, key="two", kind="decision", by="someone", via="manual"))

    # ── read paths ──
    cur.execute("SELECT count(*) FROM v_observations WHERE subject_id=%s", (co,))
    check("v_observations shows the two observations", cur.fetchone()[0] == 2)
    cur.execute("SELECT evidence_source FROM v_observations WHERE id=%s", (obs_call,))
    check("v_observations names the evidence source", cur.fetchone()[0] == "zz_test /x")
    cur.execute("SELECT reason, decided_by, via, cardinality(basis) FROM v_decisions WHERE id=%s", (dec,))
    check("v_decisions shows reason, decider, via and basis",
          cur.fetchone() == ("zz test reason", "someone", "manual", 2))
    cur.execute("SELECT subject_type FROM v_decisions WHERE subject_id=%s", (mt,))
    check("v_decisions types a meeting subject as 'meeting'", cur.fetchone() == ("meeting",))
    cur.execute("SELECT monitoring_decision, monitoring_reason, monitoring_decided_by "
                "FROM v_company_monitoring_vs_client WHERE company_id=%s", (co,))
    check("v_company_monitoring_vs_client carries the monitoring decision and reason",
          cur.fetchone() == ("do_not_monitor", "zz test reason", "someone"))
    cur.execute("SELECT count(*) FROM v_company_monitoring_vs_client WHERE company_id=%s", (co2,))
    check("a company that is not a competitor stays out of the monitoring view", cur.fetchone()[0] == 0)

    return total, fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="Postgres URL. Overrides MARKETBASE_URL.")
    ap.add_argument("--instance", help="Named MarketBase instance (written, then rolled back).")
    args = ap.parse_args()
    conn = _connect(args)
    try:
        total, fails = run(conn)
    finally:
        conn.rollback()
        conn.close()
        print("rolled back: nothing was written")
    if fails:
        print(f"check_assertion_kinds: {len(fails)} of {total} FAILED: {fails}")
        return 1
    print(f"check_assertion_kinds: {total} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
