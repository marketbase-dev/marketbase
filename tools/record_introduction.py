#!/usr/bin/env python3
"""record_introduction.py — who introduced or nominated whom (schema 099).

  add    --client X --kind introduced|nominated --by URL --whom URL
         [--to URL | --to-operator NAME] [--for-collective ID] [--purpose TEXT] [--on YYYY-MM-DD]
         --source WHO [--note TEXT] [--create]
         Records one introduction or nomination. People are found by LinkedIn URL only. A person
         who is not on file is refused, unless --create: then a person is added with the URL alone
         and a manual_add source naming this tool, for an enrichment run to fill in.
         Recording the same introduction again writes nothing.
  list   --client X [--person URL]
         Introductions made by, about or to one person, or all of them.

People are stored by an identity (their LinkedIn URL when it is on file), so a later merge of
either person keeps the row pointing at the right one.

  python3 record_introduction.py add --client "<Name>" --kind nominated \
      --by https://www.linkedin.com/in/a --whom https://www.linkedin.com/in/b \
      --to-operator "<operator display name>" --purpose speaker --source "<who said so> (<where>, <date>)"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, lead_id_for_linkedin_url, normalize_linkedin_url  # noqa: E402

ME = "record_introduction.py"
PRIORITY = {"linkedin_url": 0, "email": 1, "phone": 2, "whatsapp": 3, "linkedin_urn": 4, "whatsapp_lid": 5}


def person(cur, url: str, create: bool, why: str) -> tuple[str, str, str]:
    """(lead id, identity kind, identity value) for a LinkedIn URL."""
    norm = normalize_linkedin_url(url)
    if not norm:
        sys.exit(f"not a LinkedIn profile URL: {url}")
    lead = lead_id_for_linkedin_url(cur, norm)
    if lead:
        cur.execute("SELECT COALESCE(merged_into, id)::text FROM leads WHERE id = %s", (lead,))
        lead = cur.fetchone()[0]
    elif create:
        cur.execute("INSERT INTO leads (linkedin_url) VALUES (%s) RETURNING id::text", (norm,))
        lead = cur.fetchone()[0]
        cur.execute("""INSERT INTO lead_sources (lead_id, source_type, source_label, source_date, raw_data)
                       VALUES (%s, 'manual_add', %s, current_date, %s::jsonb)""",
                    (lead, why, json.dumps({"_source": ME, "linkedin_url": norm})))
        print(f"  added {norm} (lead {lead})")
    else:
        sys.exit(f"nobody on file for {norm}; pass --create to add them")
    cur.execute("SELECT kind, value FROM lead_identities WHERE lead_id = %s", (lead,))
    ids = cur.fetchall()
    if (("linkedin_url", norm)) in ids:
        return lead, "linkedin_url", norm
    if not ids:
        sys.exit(f"{norm}: lead {lead} has no identity on file")
    kind, value = min(ids, key=lambda kv: (PRIORITY.get(kv[0], 9), kv[1]))
    return lead, kind, value


def cmd_add(conn, a) -> int:
    cur = conn.cursor()
    why = f"{a.kind} by {a.by}" + (f" for {a.purpose}" if a.purpose else "") + f" ({a.source})"
    by = person(cur, a.by, a.create, why)
    whom = person(cur, a.whom, a.create, why)
    if by[0] == whom[0]:
        sys.exit("by and whom are the same person")
    to = person(cur, a.to, a.create, why) if a.to else None
    if a.to_operator:
        cur.execute("SELECT count(*) FROM outbound_operators WHERE display_name = %s", (a.to_operator,))
        if not cur.fetchone()[0]:
            sys.exit(f"no operator named {a.to_operator!r}")
    if a.for_collective:
        cur.execute("SELECT name FROM collectives WHERE id = %s", (a.for_collective,))
        if not cur.fetchone():
            sys.exit(f"no collective {a.for_collective}")
    cur.execute("""
        INSERT INTO lead_introductions (kind, by_kind, by_value, whom_kind, whom_value, to_kind, to_value, to_operator,
                                        for_collective_id, purpose, happened_on, source, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT ON CONSTRAINT uq_lead_introductions DO NOTHING
        RETURNING id::text""",
                (a.kind, by[1], by[2], whom[1], whom[2], to[1] if to else None, to[2] if to else None,
                 a.to_operator, a.for_collective, a.purpose, a.on, a.source, a.note))
    row = cur.fetchone()
    # invariant, inside the transaction: exactly one row now says this, and it resolves to both people
    cur.execute("""SELECT count(*) FROM v_lead_introductions
                    WHERE kind = %s AND by_lead_id = %s AND whom_lead_id = %s
                      AND for_collective_id IS NOT DISTINCT FROM %s AND purpose IS NOT DISTINCT FROM %s""",
                (a.kind, by[0], whom[0], a.for_collective, a.purpose))
    n = cur.fetchone()[0]
    if n < 1:
        conn.rollback()
        sys.exit(f"rolled back: the introduction does not read back ({n} rows)")
    conn.commit()
    print(f"{'recorded' if row else 'already on file'}: {a.kind} {a.by} -> {a.whom}"
          + (f" to {a.to or a.to_operator}" if (a.to or a.to_operator) else "")
          + (f" for {a.purpose}" if a.purpose else ""))
    return 0


def cmd_list(conn, a) -> int:
    cur = conn.cursor()
    where, args = "", []
    if a.person:
        lead = lead_id_for_linkedin_url(cur, normalize_linkedin_url(a.person))
        if not lead:
            sys.exit(f"nobody on file for {a.person}")
        where, args = "WHERE %s IN (by_lead_id, whom_lead_id, to_lead_id)", [lead]
    cur.execute(f"""SELECT kind, by_name, whom_name, to_name, for_collective, purpose, happened_on, source
                      FROM v_lead_introductions {where} ORDER BY recorded_at""", args)
    rows = cur.fetchall()
    for kind, by, whom, to, coll, purpose, on, source in rows:
        print(f"  {by or '?'} {kind} {whom or '?'}" + (f" to {to}" if to else "") + (f" for {purpose}" if purpose else "")
              + (f" ({coll})" if coll else "") + (f" on {on}" if on else "") + f"  [{source}]")
    print(f"{len(rows)} introduction(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add")
    s.add_argument("--client", required=True)
    s.add_argument("--kind", required=True, choices=["introduced", "nominated"])
    s.add_argument("--by", required=True, help="LinkedIn URL of the person who made it")
    s.add_argument("--whom", required=True, help="LinkedIn URL of the person it was about")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--to", help="LinkedIn URL of the person they were introduced to")
    g.add_argument("--to-operator", help="display name of the operator they were introduced to")
    s.add_argument("--for-collective", help="id of the event or group it was for")
    s.add_argument("--purpose", help='what for, in words: "speaker", "customer intro"')
    s.add_argument("--on", help="the date it happened, YYYY-MM-DD, when known")
    s.add_argument("--source", required=True, help="who reported it, and where")
    s.add_argument("--note")
    s.add_argument("--create", action="store_true", help="add a person who is not on file")
    s = sub.add_parser("list")
    s.add_argument("--client", required=True)
    s.add_argument("--person", help="LinkedIn URL")
    a = ap.parse_args()
    conn = connect(a.client)
    try:
        return {"add": cmd_add, "list": cmd_list}[a.cmd](conn, a)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
