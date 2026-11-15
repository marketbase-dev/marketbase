#!/usr/bin/env python3
"""operator_chat_settings.py — what an operator's chat sync stores (schema 094).

  policy   --client X --operator <name|id> --direct yes|no --groups monitored_only|all|none
           --backfill all_history|from_first_sync --decided-by WHO [--note TEXT]
           Sets the operator's operator_sync_policy row (one per operator).
  monitor  --client X --operator <name|id> --chat <provider chat id> [--chat ...] (--on | --off)
           --decided-by WHO [--via manual|page] [--note TEXT]
           Records a monitor or stop decision per group, append-only. A group whose current
           decision already says the same is skipped, so re-sending a whole selection is safe.
           Monitoring a group makes the next sync_operator_chats.py run backfill its history.
  show     --client X --operator <name|id> [--json]
           The policy and the groups currently monitored.

The operator is matched by id, or by display name together with --channel (default whatsapp);
a name that matches more than one operator is refused.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect  # noqa: E402

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def operator(cur, value: str, channel: str) -> tuple[str, str]:
    if UUID.match(value):
        cur.execute("SELECT id::text, display_name FROM outbound_operators WHERE id = %s", (value,))
    else:
        cur.execute("SELECT id::text, display_name FROM outbound_operators WHERE display_name = %s AND channel = %s",
                    (value, channel))
    rows = cur.fetchall()
    if len(rows) != 1:
        sys.exit(f"operator {value!r} ({channel}): {len(rows)} matches, expected exactly 1")
    return rows[0]


def cmd_policy(conn, a) -> int:
    cur = conn.cursor()
    op_id, name = operator(cur, a.operator, a.channel)
    cur.execute("""
        INSERT INTO operator_sync_policy (operator_id, store_direct_messages, store_group_messages, backfill,
                                          decided_by, note)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (operator_id) DO UPDATE SET
            store_direct_messages = EXCLUDED.store_direct_messages,
            store_group_messages = EXCLUDED.store_group_messages, backfill = EXCLUDED.backfill,
            decided_by = EXCLUDED.decided_by, decided_at = now(), note = EXCLUDED.note""",
        (op_id, a.direct == "yes", a.groups, a.backfill, a.decided_by, a.note))
    if cur.rowcount != 1:
        conn.rollback()
        sys.exit(f"expected 1 policy row, got {cur.rowcount}")
    conn.commit()
    print(f"{name}: direct={a.direct} groups={a.groups} backfill={a.backfill} (decided by {a.decided_by})")
    return 0


def cmd_monitor(conn, a) -> int:
    cur = conn.cursor()
    op_id, name = operator(cur, a.operator, a.channel)
    want = a.on
    # One decision per chat per run: decisions in one transaction share now(), so two for the
    # same chat would leave the view to break the tie on a random id.
    chats = list(dict.fromkeys(a.chat))
    cur.execute("""SELECT c.provider_chat_id, c.id::text, c.kind, c.title, m.monitor
                     FROM operator_chats c LEFT JOIN v_operator_chat_monitoring m ON m.chat_id = c.id
                    WHERE c.operator_id = %s AND c.provider_chat_id = ANY(%s)""", (op_id, chats))
    found = {r[0]: r for r in cur.fetchall()}
    missing = [x for x in chats if x not in found]
    direct = [x for x in chats if x in found and found[x][2] != "group"]
    if missing or direct:
        sys.exit(f"refused: {len(missing)} chat id(s) unknown for {name} {missing[:5]}, "
                 f"{len(direct)} not groups {direct[:5]}; nothing written")
    todo = [found[x][1] for x in chats if found[x][4] is not want]   # no decision yet counts as a change
    written = 0
    for chat_id in todo:
        cur.execute("""INSERT INTO operator_chat_monitoring (chat_id, monitor, decided_by, via, note)
                       VALUES (%s, %s, %s, %s, %s)""", (chat_id, want, a.decided_by, a.via, a.note))
        written += cur.rowcount
    # Invariant, inside the transaction: every requested chat now reads as requested.
    cur.execute("""SELECT count(*) FILTER (WHERE m.monitor IS NOT DISTINCT FROM %s), count(*)
                     FROM operator_chats c LEFT JOIN v_operator_chat_monitoring m ON m.chat_id = c.id
                    WHERE c.operator_id = %s AND c.provider_chat_id = ANY(%s)""", (want, op_id, chats))
    ok, total = cur.fetchone()
    if written != len(todo) or ok != total or total != len(chats):
        conn.rollback()
        sys.exit(f"rolled back: wrote {written} of {len(todo)}; {ok} of {total} chats read as requested")
    conn.commit()
    print(f"{name}: {'monitor' if want else 'stop'} {written} group(s); "
          f"{len(chats) - written} already {'monitored' if want else 'not monitored'}")
    return 0


def cmd_show(conn, a) -> int:
    cur = conn.cursor()
    op_id, name = operator(cur, a.operator, a.channel)
    cur.execute("""SELECT store_direct_messages, store_group_messages, backfill, decided_by, decided_at::text
                     FROM operator_sync_policy WHERE operator_id = %s""", (op_id,))
    pol = cur.fetchone()
    cur.execute("""SELECT c.provider_chat_id, c.title, m.decided_by, m.decided_at::text
                     FROM v_operator_chat_monitoring m JOIN operator_chats c ON c.id = m.chat_id
                    WHERE c.operator_id = %s AND m.monitor ORDER BY c.title""", (op_id,))
    mon = cur.fetchall()
    if a.json:
        print(json.dumps({"operator": name, "policy": None if not pol else dict(zip(
            ("store_direct_messages", "store_group_messages", "backfill", "decided_by", "decided_at"), pol)),
            "monitored": [r[0] for r in mon]}, default=str))
        return 0
    print(f"{name}: policy {pol if pol else 'none (nothing syncs)'}")
    print(f"monitored groups: {len(mon)}")
    for pcid, title, by, at in mon:
        print(f"  {title} ({pcid}) since {at[:10]} by {by}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("policy", "monitor", "show"):
        s = sub.add_parser(name)
        s.add_argument("--client", required=True)
        s.add_argument("--operator", required=True, help="operator id, or display name with --channel")
        s.add_argument("--channel", default="whatsapp")
        if name == "policy":
            s.add_argument("--direct", choices=["yes", "no"], required=True)
            s.add_argument("--groups", choices=["monitored_only", "all", "none"], required=True)
            s.add_argument("--backfill", choices=["all_history", "from_first_sync"], required=True)
            s.add_argument("--decided-by", required=True)
            s.add_argument("--note")
        if name == "monitor":
            s.add_argument("--chat", action="append", required=True, help="provider chat id; repeat")
            g = s.add_mutually_exclusive_group(required=True)
            g.add_argument("--on", action="store_true", default=None)
            g.add_argument("--off", dest="on", action="store_false")
            s.add_argument("--decided-by", required=True)
            s.add_argument("--via", choices=["manual", "page"], default="manual")
            s.add_argument("--note")
        if name == "show":
            s.add_argument("--json", action="store_true")
    a = ap.parse_args()
    conn = connect(a.client)
    try:
        return {"policy": cmd_policy, "monitor": cmd_monitor, "show": cmd_show}[a.cmd](conn, a)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
