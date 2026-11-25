#!/usr/bin/env python3
"""Exercise the conversion of old company_relationships rows into the
company-link format (schema/103) against a database that already has every
migration applied. Everything runs inside ONE transaction that is always rolled
back: no row survives, on any instance.

Synthetic old rows of every shape the instances hold are converted, then:
  * the new format in the old table's shape equals the old table on every state
    and text column (relationship, scope, status, monitor, client choice and its
    source, discovery, source, notes, recorded_at);
  * a decision is invented nowhere: an old monitor_desired with no decision on
    file, a not_offered, a status stay unclassified; a company-level monitor
    decision on file is copied onto the link as a decision; a client's
    selected/deselected is the client's decision citing its source;
  * a second run writes nothing; nothing is deleted or changed in the old table;
  * a link that predates markers gets one, from its first claim.

Usage:
  python3 scripts/check_company_relationship_conversion.py          # MARKETBASE_URL (CI)
  python3 scripts/check_company_relationship_conversion.py --url postgresql://...
"""
from __future__ import annotations

import argparse
import json
import os
import sys

CHECK_VIOLATION = "23514"
WRITER = "check_company_relationship_conversion"
CLIENT = "ZZ Acme"

STATE_COLS = ("company_id", "relationship", "scope", "status", "monitor_desired", "rep_discovery",
              "discovery_config", "client_disposition", "client_disposition_source", "source", "notes",
              "recorded_at")


def _connect(args):
    url = args.url or os.environ.get("MARKETBASE_URL")
    if not url:
        sys.exit("No database. Pass --url or set MARKETBASE_URL.")
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

    def refused(name, sql, params=(), needle=None):
        cur.execute("SAVEPOINT k")
        try:
            cur.execute(sql, params)
            cur.execute("RELEASE SAVEPOINT k")
            check(name, False, "accepted")
        except Exception as e:  # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT k")
            msg = str(e).splitlines()[0]
            check(name, _sqlstate(e) == CHECK_VIOLATION and (needle is None or needle in msg), msg)

    def one(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchone()

    def company(slug, name):
        return one("INSERT INTO companies (linkedin_slug, name) VALUES (%s, %s) RETURNING id", (slug, name))[0]

    def legacy(co, rel, **kw):
        cols = {"company_id": co, "relationship": rel, "source": kw.pop("source", "zz-research-tool"),
                "recorded_at": kw.pop("recorded_at", "2026-01-02T03:04:05+00")}
        cols.update(kw)
        names = ", ".join(cols)
        marks = ", ".join(["%s"] * len(cols))
        return one(f"INSERT INTO company_relationships ({names}) VALUES ({marks}) RETURNING id",
                   [json.dumps(v) if isinstance(v, dict) else v for v in cols.values()])[0]

    def link_claims(co, key):
        cur.execute("""SELECT a.value, a.kind, a.decided_by, a.via, a.notes FROM assertions a
                         JOIN company_edges e ON e.id = a.subject_company_edge_id
                        WHERE e.from_company_id = %s AND a.key = %s AND a.removed_at IS NULL""", (co, key))
        return cur.fetchall()

    # ── fixtures: one company per shape ──
    own = company("zz-conv-acme", CLIENT)
    brand = company("zz-conv-acme-brand", "ZZ Acme Brand")
    rival = company("zz-conv-rival", "ZZ Rival")
    maybe = company("zz-conv-maybe", "ZZ Maybe")
    vendor = company("zz-conv-vendor", "ZZ Vendor")
    dropped = company("zz-conv-dropped", "ZZ Dropped")
    decided = company("zz-conv-decided", "ZZ Decided")
    acquired = company("zz-conv-acquired", "ZZ Acquired Brand")
    prospect = company("zz-conv-prospect", "ZZ Prospect")
    unrelated = company("zz-conv-unrelated", "ZZ Unrelated")

    rows = {
        "self": legacy(own, "self", source="manual", notes="our own company"),
        "brand": legacy(brand, "self", source="manual"),
        "rival": legacy(rival, "competitor", scope="direct", monitor_desired=True,
                        monitor_desired_at="2026-02-01T00:00:00+00", client_disposition="selected",
                        client_disposition_at="2026-02-02", client_disposition_source="selection workbook, row 2",
                        notes="the main rival"),
        "maybe": legacy(maybe, "suspected_competitor", scope="adjacent", client_disposition="not_offered"),
        "vendor": legacy(vendor, "security_vendor", scope="inferred", status="checking"),
        "dropped": legacy(dropped, "competitor", scope="direct", monitor_desired=False,
                          monitor_desired_at="2026-03-01T00:00:00+00", client_disposition="deselected",
                          client_disposition_at="2026-03-02", client_disposition_source="selection workbook, row 9"),
        "decided": legacy(decided, "competitor", scope="direct", monitor_desired=True,
                          monitor_desired_at="2026-01-05T00:00:00+00"),
        "acquired": legacy(acquired, "competitor", scope="direct", rep_discovery="acquired_brand",
                           discovery_config={"brand": "zz", "parents": ["ZZ Parent"], "product_keywords": ["zz"]}),
        "prospect": legacy(prospect, "prospect_candidate", scope="direct"),
    }
    # a company-level monitor decision on file for one of them
    cur.execute("""INSERT INTO assertions (subject_company_id, namespace, key, value, kind, decided_by, via, notes,
                                           asserted_by, decided_at)
                   VALUES (%s, 'monitoring', 'monitor', 'monitor', 'decision', 'the operator', 'manual',
                           'the client chose it at onboarding', %s, '2026-01-06T00:00:00+00')""", (decided, WRITER))
    # a link that predates markers: a scope claim but no marker
    old_link = one("INSERT INTO company_edges (from_company_id, to_company_id, relationship, first_seen_at) "
                   "VALUES (%s, %s, 'competitor', '2025-12-01T00:00:00+00') RETURNING id", (unrelated, prospect))[0]
    cur.execute("INSERT INTO assertions (subject_company_edge_id, namespace, key, value, asserted_by, asserted_at) "
                "VALUES (%s, 'relationship', 'scope', 'direct', 'zz-sizing-tool@1.0', '2025-12-01T00:00:00+00')",
                (old_link,))
    cur.execute("SELECT md5(string_agg(t::text, '|' ORDER BY t.id)) FROM company_relationships t")
    legacy_digest = cur.fetchone()[0]

    # ── refusals ──
    refused("converting before the own company is marked is refused",
            "SELECT convert_company_relationship(%s, %s, %s)", (rows["rival"], WRITER, CLIENT), needle="own company")
    refused("a self row with no own-company mark is refused",
            "SELECT convert_company_relationship(%s, %s, %s)", (rows["self"], WRITER, CLIENT), needle="own-company mark")
    refused("own company must be primary or brand",
            "SELECT set_own_company(%s, 'main', %s, NULL)", (own, WRITER))

    cur.execute("SELECT set_own_company(%s, 'primary', 'manual', 'our own company', '2026-01-02T03:04:05+00')", (own,))
    check("marking the primary own company", cur.fetchone()[0] == "inserted")
    cur.execute("SELECT set_own_company(%s, 'primary', 'manual', 'our own company', '2026-01-02T03:04:05+00')", (own,))
    check("marking it again is a no-op", cur.fetchone()[0] == "unchanged")
    refused("a second primary is refused",
            "SELECT set_own_company(%s, 'primary', %s, NULL)", (brand, WRITER), needle="already has a primary")
    cur.execute("SELECT set_own_company(%s, 'brand', 'manual', NULL, '2026-01-02T03:04:05+00')", (brand,))
    check("another own brand is marked brand", cur.fetchone()[0] == "inserted")

    # ── convert everything ──
    n_before = one("SELECT count(*) FROM assertions")[0]
    out = {}
    for name, rid in rows.items():
        cur.execute("SELECT convert_company_relationship(%s, %s, %s)", (rid, WRITER, CLIENT))
        out[name] = cur.fetchone()[0]
    check("every non-self row converts",
          all(out[k]["outcome"] == "converted" for k in rows if k not in ("self", "brand")),
          {k: v["outcome"] for k, v in out.items()})
    check("self rows are already converted by their own-company marks",
          out["self"]["outcome"] == "already converted" and out["brand"]["outcome"] == "already converted")
    cur.execute("SELECT mark_unmarked_link(%s, %s)", (old_link, WRITER))
    check("a link that predates markers gets one", cur.fetchone()[0] == "marked")
    mk = one("SELECT asserted_by, (asserted_at AT TIME ZONE 'UTC')::date FROM assertions WHERE subject_company_edge_id = %s "
             "AND namespace = 'relationship' AND key = 'competitor' AND removed_at IS NULL", (old_link,))
    check("its writer and date come from its first claim and the link", mk == ("zz-sizing-tool@1.0",
                                                                               __import__("datetime").date(2025, 12, 1)), mk)
    n_after = one("SELECT count(*) FROM assertions")[0]

    # ── what was written ──
    check("the possible competitor is a competitor link marked suspected",
          one("""SELECT a.confidence FROM assertions a JOIN company_edges e ON e.id = a.subject_company_edge_id
                  WHERE e.from_company_id = %s AND a.namespace = 'relationship' AND a.key = 'competitor'
                    AND a.removed_at IS NULL""", (maybe,)) == ("suspected",))
    m = link_claims(rival, "monitor")
    check("monitor_desired with no decision on file stays unclassified",
          len(m) == 1 and m[0][0] == "monitor" and m[0][1] is None and m[0][2] is None, m)
    m = link_claims(decided, "monitor")
    check("a company-level monitor decision is copied onto the link as a decision",
          len(m) == 1 and m[0][1] == "decision" and m[0][2] == "the operator" and m[0][3] == "manual", m)
    check("the company-level decision stays active (its readers move in ticket 03)",
          one("SELECT count(*) FROM assertions WHERE subject_company_id = %s AND namespace = 'monitoring' "
              "AND removed_at IS NULL", (decided,))[0] == 1)
    m = link_claims(dropped, "monitor")
    check("a false monitor_desired with a date is an unclassified do_not_monitor, never a decision",
          len(m) == 1 and m[0][0] == "do_not_monitor" and m[0][1] is None, m)
    check("a default false monitor_desired (no date) writes nothing", link_claims(vendor, "monitor") == [])
    c = link_claims(rival, "choice")
    check("selected is the client's decision citing its source",
          len(c) == 1 and c[0][1] == "decision" and c[0][2] == f"{CLIENT} (the client)" and c[0][3] == "reviewed_batch"
          and c[0][4] == "selection workbook, row 2", c)
    c = link_claims(maybe, "choice")
    check("not_offered is never a decision", len(c) == 1 and c[0][0] == "not_offered" and c[0][1] is None, c)
    s = link_claims(vendor, "status")
    check("a status with no decider stays unclassified", len(s) == 1 and s[0][0] == "checking" and s[0][1] is None, s)

    # ── the old table's shape ──
    cols = ", ".join(STATE_COLS)
    cur.execute(f"""SELECT count(*) FROM (
                      (SELECT {cols} FROM company_relationships EXCEPT SELECT {cols} FROM v_company_relationships_from_links)
                      UNION ALL
                      (SELECT {cols} FROM v_company_relationships_from_links EXCEPT SELECT {cols} FROM company_relationships)) d""")
    n = cur.fetchone()[0]
    if n:
        cur.execute(f"SELECT {cols} FROM company_relationships EXCEPT SELECT {cols} FROM v_company_relationships_from_links")
        print("   only in the old table:", cur.fetchall())
        cur.execute(f"SELECT {cols} FROM v_company_relationships_from_links EXCEPT SELECT {cols} FROM company_relationships")
        print("   only in the new format:", cur.fetchall())
    check("the new format equals the old table on every state and text column", n == 0, f"{n} differing rows")
    diff = one("""SELECT count(*) FROM company_relationships t JOIN v_company_relationships_from_links v
                    ON v.company_id = t.company_id AND v.relationship = t.relationship
                   WHERE v.monitor_desired_at IS DISTINCT FROM t.monitor_desired_at""")[0]
    check("monitor dates differ only where a dated decision replaced the old date (1 here)", diff == 1, diff)

    # ── idempotent, nothing deleted, the old table untouched ──
    for name, rid in rows.items():
        cur.execute("SELECT convert_company_relationship(%s, %s, %s)", (rid, WRITER, CLIENT))
        o = cur.fetchone()[0]["outcome"]
        if o != "already converted":
            check(f"second run of {name} writes nothing", False, o)
    cur.execute("SELECT mark_unmarked_link(%s, %s)", (old_link, WRITER))
    check("second run: every row already converted, marker already there",
          cur.fetchone()[0] == "already marked" and one("SELECT count(*) FROM assertions")[0] == n_after)
    check("the conversion only added claims", n_after > n_before)
    cur.execute("SELECT md5(string_agg(t::text, '|' ORDER BY t.id)) FROM company_relationships t")
    check("the old table is byte-for-byte unchanged", cur.fetchone()[0] == legacy_digest)

    # ── the door still writes decisions only ──
    link = one("SELECT link_id FROM company_link_state WHERE from_company_id = %s", (rival,))[0]
    refused("set_link_decision without a decider is refused (the registry no longer forces it)",
            "SELECT set_link_decision(%s, 'monitor', 'monitor', NULL, 'manual', 'r', %s)", (link, WRITER),
            needle="needs decided_by")
    cur.execute("SELECT set_link_decision(%s, 'monitor', 'do_not_monitor', 'the operator', 'manual', 'stop', %s)",
                (link, WRITER))
    check("a door decision after conversion supersedes the converted claim and the old row follows",
          one("SELECT monitor_desired FROM company_relationships WHERE id = %s", (rows["rival"],)) == (False,))
    return total, fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
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
