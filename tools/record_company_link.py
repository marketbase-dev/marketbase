#!/usr/bin/env python3
"""record_company_link.py — a person who may be linked to a company (schema 100).

  add     --client X --person URL --company-id ID [--company-id ID ...]
          --link-type portfolio_company --via-company-id ID [--certainty possible|confirmed]
          --source WHO [--note TEXT] [--execute]
          Links one person to one or more companies, by company id (never by name). The person is
          found by LinkedIn URL and must be on file. A link already on file is left alone.
          Without --execute it is a dry run: it prints what it would write, then rolls back.
  retire  --client X --id ID --reason TEXT [--execute]
          Retires one link, by the id `list` prints. Nothing is deleted.
  list    --client X [--person URL | --company-id ID]
          Active links for one person or one company, or all of them, each with its id.

  python3 record_company_link.py add --client "<Name>" --person https://www.linkedin.com/in/dana \
      --company-id <id> --company-id <id> --link-type portfolio_company --via-company-id <fund id> \
      --source "<who said so> (<where>, <date>)" --execute

portfolio_company says the company is in the portfolio of the fund the person works at. It does
not say the person made the investment.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, lead_id_for_linkedin_url, normalize_linkedin_url  # noqa: E402
from record_introduction import person  # noqa: E402  (same identity choice as 099)

LINK_TYPES = ("portfolio_company",)


def company_name(cur, cid: str) -> str:
    cur.execute("SELECT name FROM companies WHERE id = %s", (cid,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"no company {cid}")
    return row[0]


def cmd_add(conn, a) -> int:
    cur = conn.cursor()
    lead, kind, value = person(cur, a.person, False, "")
    via = company_name(cur, a.via_company_id) if a.via_company_id else None
    if a.link_type == "portfolio_company" and not via:
        sys.exit("portfolio_company needs --via-company-id (the fund)")
    targets = list(dict.fromkeys(a.company_id))
    names = {cid: company_name(cur, cid) for cid in targets}
    written = 0
    for cid in targets:
        cur.execute("""SELECT id::text FROM lead_company_links
                        WHERE person_kind = %s AND person_value = %s AND company_id = %s AND link_type = %s
                          AND via_company_id IS NOT DISTINCT FROM %s AND retired_at IS NULL""",
                    (kind, value, cid, a.link_type, a.via_company_id))
        if cur.fetchone():
            print(f"  already on file: {names[cid]}")
            continue
        cur.execute("""INSERT INTO lead_company_links (person_kind, person_value, company_id, link_type, certainty,
                                                       via_company_id, source, note)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (kind, value, cid, a.link_type, a.certainty, a.via_company_id, a.source, a.note))
        if cur.rowcount != 1:
            conn.rollback()
            sys.exit(f"rolled back: expected 1 row for {names[cid]}, got {cur.rowcount}")
        written += 1
        print(f"  link: {names[cid]} ({a.link_type}, {a.certainty}" + (f", via {via}" if via else "") + ")")
    # invariant, inside the transaction: every company reads back through the view, once, for this person
    cur.execute("""SELECT company_id::text, count(*) FROM v_lead_company_links
                    WHERE person_lead_id = %s AND link_type = %s AND company_id = ANY(%s::uuid[])
                      AND via_company_id IS NOT DISTINCT FROM %s
                    GROUP BY 1""", (lead, a.link_type, targets, a.via_company_id))
    got = dict(cur.fetchall())
    bad = [names[c] for c in targets if got.get(c) != 1]
    if bad:
        conn.rollback()
        sys.exit(f"rolled back: these do not read back exactly once: {bad}")
    if a.execute:
        conn.commit()
        print(f"{written} written, {len(targets) - written} already on file")
    else:
        conn.rollback()
        print(f"dry run: {written} would be written, {len(targets) - written} already on file; rolled back")
    return 0


def cmd_retire(conn, a) -> int:
    cur = conn.cursor()
    cur.execute("SELECT person_name, company_name, link_type FROM v_lead_company_links WHERE id = %s", (a.id,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"no active link {a.id}")
    cur.execute("""UPDATE lead_company_links SET retired_at = now(), retired_reason = %s
                    WHERE id = %s AND retired_at IS NULL""", (a.reason, a.id))
    if cur.rowcount != 1:
        conn.rollback()
        sys.exit(f"rolled back: expected 1 row, got {cur.rowcount}")
    print(f"  retire: {row[0]} -> {row[1]} ({row[2]}): {a.reason}")
    conn.commit() if a.execute else conn.rollback()
    print("retired" if a.execute else "dry run, rolled back")
    return 0


def cmd_list(conn, a) -> int:
    cur = conn.cursor()
    where, args = "", []
    if a.person:
        lead = lead_id_for_linkedin_url(cur, normalize_linkedin_url(a.person))
        if not lead:
            sys.exit(f"nobody on file for {a.person}")
        where, args = "WHERE person_lead_id = %s", [lead]
    elif a.company_id:
        where, args = "WHERE company_id = %s", [a.company_id]
    cur.execute(f"""SELECT id::text, person_name, company_name, link_type, certainty, via_company, source
                      FROM v_lead_company_links {where} ORDER BY person_name, company_name""", args)
    rows = cur.fetchall()
    for lid, pname, cname, ltype, cert, via, source in rows:
        print(f"  {pname or '?'} -> {cname}  {ltype}, {cert}" + (f", via {via}" if via else "")
              + f"  [{source}]  id {lid}")
    print(f"{len(rows)} link(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add")
    s.add_argument("--client", required=True)
    s.add_argument("--person", required=True, help="LinkedIn URL of the person")
    s.add_argument("--company-id", required=True, action="append", help="companies.id; repeat for several")
    s.add_argument("--link-type", required=True, choices=LINK_TYPES)
    s.add_argument("--via-company-id", help="companies.id of the company that carries the link (the fund)")
    s.add_argument("--certainty", default="possible", choices=["possible", "confirmed"])
    s.add_argument("--source", required=True, help="who reported it, and where")
    s.add_argument("--note")
    s.add_argument("--execute", action="store_true", help="write (default: dry run)")
    s = sub.add_parser("retire")
    s.add_argument("--client", required=True)
    s.add_argument("--id", required=True, help="the link's id, as `list` prints it")
    s.add_argument("--reason", required=True)
    s.add_argument("--execute", action="store_true", help="write (default: dry run)")
    s = sub.add_parser("list")
    s.add_argument("--client", required=True)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--person", help="LinkedIn URL")
    g.add_argument("--company-id")
    a = ap.parse_args()
    conn = connect(a.client)
    try:
        return {"add": cmd_add, "retire": cmd_retire, "list": cmd_list}[a.cmd](conn, a)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
