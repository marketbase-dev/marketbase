#!/usr/bin/env python3
"""sync_operator_chats.py — an operator's own messaging chats into MarketBase (schema 093).

For each active `outbound_operators` row on a messaging channel (`whatsapp`) that has an
`operator_sync_policy` row:

  1. chats     page the account's chats from Unipile and upsert `operator_chats`.
  2. members   for every group, monitored or not, upsert `operator_chat_members` from the
               member list, and stamp `left_at` on anyone no longer listed (nobody is
               deleted). A chat is refreshed only when it is new or has had activity since
               its last member sync. For a direct chat, the other person.
  3. messages  for direct chats when the policy stores them, and for the groups it stores
               (`monitored_only`: the latest `v_operator_chat_monitoring` decision is to
               monitor; `all`; `none`). Messages arrive newest first. The first sync of a
               chat pages back to the provider's oldest message when the policy backfills
               `all_history` (then `history_complete`); later syncs stop at the first page
               that holds nothing new.

Every provider object is stored raw on the row it creates, and the run commits per page, so a
killed run resumes where it stopped. Leads are never created; people link to leads through
`lead_identities` (kind `whatsapp`) elsewhere.

Unipile credentials come from the instance's own scope: UNIPILE_BASE_SERVER +
UNIPILE_ACCESS_TOKEN, or UNIPILE_BASE_URL + UNIPILE_API_KEY.

Usage:
  python3 sync_operator_chats.py --client <Name>                 # every stage, every operator
  python3 sync_operator_chats.py --client <Name> --only chats,members
  python3 sync_operator_chats.py --all                           # every instance with a policy
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, discover_instances, load_client_env  # noqa: E402

CHANNELS = {"whatsapp": "WHATSAPP"}
PAGE = 250
PHONE_NAME = re.compile(r"^\+?[\d\s()-]{6,}$")


class Unipile:
    def __init__(self, env: dict):
        if env.get("UNIPILE_ACCESS_TOKEN") and env.get("UNIPILE_BASE_SERVER"):
            self.base, self.key = f"https://{env['UNIPILE_BASE_SERVER']}", env["UNIPILE_ACCESS_TOKEN"]
        elif env.get("UNIPILE_API_KEY") and env.get("UNIPILE_BASE_URL"):
            self.base, self.key = env["UNIPILE_BASE_URL"].rstrip("/"), env["UNIPILE_API_KEY"]
        else:
            raise SystemExit("no Unipile credentials in this instance's scope")
        self.calls = 0

    def get(self, path: str, **qs) -> dict:
        err = None
        for backoff in (0, 5, 30, 90):
            time.sleep(backoff)
            req = urllib.request.Request(f"{self.base}{path}?" + urllib.parse.urlencode(qs),
                                         headers={"X-API-KEY": self.key, "accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    self.calls += 1
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code} {e.read()[:200].decode(errors='ignore')}"
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except Exception as e:  # noqa: BLE001  transport errors on long runs
                err = f"{type(e).__name__}: {e}"
        raise RuntimeError(f"{path}: {err}")

    def pages(self, path: str, **qs):
        cursor = None
        while True:
            r = self.get(path, **qs, limit=PAGE, **({"cursor": cursor} if cursor else {}))
            yield r.get("items") or []
            cursor = r.get("cursor")
            if not cursor or not r.get("items"):
                return


def ts(v) -> str | None:
    return v if isinstance(v, str) and v else None


def operators(cur) -> list[dict]:
    cur.execute("""SELECT o.id, o.display_name, o.channel, o.unipile_account_id, p.store_direct_messages,
                          p.store_group_messages, p.backfill
                     FROM outbound_operators o JOIN operator_sync_policy p ON p.operator_id = o.id
                    WHERE o.is_active AND o.channel = ANY(%s)""", (list(CHANNELS),))
    cols = ("id", "name", "channel", "account", "store_direct", "store_groups", "backfill")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── 1. chats ───────────────────────────────────────────────────────────────────
def sync_chats(conn, u: Unipile, op: dict) -> dict:
    cur = conn.cursor()
    n = collections.Counter()
    for page in u.pages("/api/v1/chats", account_id=op["account"]):
        for ch in page:
            kind = "group" if ch.get("type") == 1 else "direct" if ch.get("type") == 0 else None
            if not kind:
                n["skipped (unknown type)"] += 1
                continue
            cur.execute("""
                INSERT INTO operator_chats (operator_id, provider_chat_id, provider_thread_id, kind, title,
                                            last_activity_at, archived, read_only, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (operator_id, provider_chat_id) DO UPDATE SET
                    title = EXCLUDED.title, last_activity_at = EXCLUDED.last_activity_at,
                    archived = EXCLUDED.archived, read_only = EXCLUDED.read_only, raw = EXCLUDED.raw
                RETURNING (xmax = 0)""",
                (op["id"], ch["id"], ch.get("provider_id"), kind, ch.get("name"), ts(ch.get("timestamp")),
                 bool(ch.get("archived")), bool(ch.get("read_only")), json.dumps(ch)))
            n["new" if cur.fetchone()[0] else "updated"] += 1
            n[kind] += 1
        conn.commit()
    return dict(n)


# ── 2. members ─────────────────────────────────────────────────────────────────
def sync_members(conn, u: Unipile, op: dict, max_chats: int | None) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT id, provider_chat_id FROM operator_chats WHERE operator_id = %s
                     AND (members_synced_at IS NULL OR last_activity_at > members_synced_at)
                   ORDER BY kind = 'group' DESC, last_activity_at DESC NULLS LAST""", (op["id"],))
    todo = cur.fetchall()[:max_chats] if max_chats else cur.fetchall()
    n = collections.Counter()
    for i, (chat_id, pcid) in enumerate(todo, 1):
        seen: dict[str, dict] = {}
        for page in u.pages(f"/api/v1/chats/{pcid}/attendees"):
            for a in page:
                pid = a.get("provider_id")
                if not pid:
                    continue
                # One person can be listed twice: once under the saved name, once as a number.
                prev = seen.get(pid)
                if prev is None or (PHONE_NAME.match(prev.get("name") or "") and not PHONE_NAME.match(a.get("name") or "")):
                    seen[pid] = a
        for pid, a in seen.items():
            spec = a.get("specifics") or {}
            phone = spec.get("phone_number") if spec.get("phone_number") not in (None, "hidden") else None
            cur.execute("""
                INSERT INTO operator_chat_members (chat_id, provider_member_id, phone, display_name, is_self, raw)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (chat_id, provider_member_id) DO UPDATE SET
                    phone = COALESCE(EXCLUDED.phone, operator_chat_members.phone),
                    display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), operator_chat_members.display_name),
                    last_seen_at = now(), left_at = NULL, raw = EXCLUDED.raw""",
                (chat_id, pid, phone, a.get("name"), bool(a.get("is_self")), json.dumps(a)))
        cur.execute("""UPDATE operator_chat_members SET left_at = now()
                        WHERE chat_id = %s AND left_at IS NULL AND NOT (provider_member_id = ANY(%s))""",
                    (chat_id, list(seen)))
        n["left"] += cur.rowcount
        cur.execute("UPDATE operator_chats SET members_synced_at = now() WHERE id = %s", (chat_id,))
        n["chats"] += 1
        n["members"] += len(seen)
        conn.commit()
        if i % 100 == 0:
            print(f"    members: {i}/{len(todo)} chats", flush=True)
    return dict(n)


# ── 3. messages ────────────────────────────────────────────────────────────────
def message_chats(cur, op: dict) -> list[tuple]:
    clauses = []
    if op["store_direct"]:
        clauses.append("c.kind = 'direct'")
    if op["store_groups"] == "all":
        clauses.append("c.kind = 'group'")
    elif op["store_groups"] == "monitored_only":
        clauses.append("(c.kind = 'group' AND m.monitor IS TRUE)")
    if not clauses:
        return []
    cur.execute(f"""SELECT c.id, c.provider_chat_id, c.history_complete, c.messages_synced_at
                      FROM operator_chats c LEFT JOIN v_operator_chat_monitoring m ON m.chat_id = c.id
                     WHERE c.operator_id = %s AND ({' OR '.join(clauses)})
                       AND (c.messages_synced_at IS NULL OR c.last_activity_at > c.messages_synced_at
                            OR NOT c.history_complete)
                     ORDER BY c.last_activity_at DESC NULLS LAST""", (op["id"],))
    return cur.fetchall()


def sync_messages(conn, u: Unipile, op: dict, max_chats: int | None) -> dict:
    cur = conn.cursor()
    todo = message_chats(cur, op)
    todo = todo[:max_chats] if max_chats else todo
    n = collections.Counter()
    for i, (chat_id, pcid, complete, synced) in enumerate(todo, 1):
        oldest = None
        reached_end = True
        first_page = True
        for page in u.pages(f"/api/v1/chats/{pcid}/messages"):
            new = 0
            for m in page:
                if not m.get("id") or not m.get("timestamp"):
                    continue
                cur.execute("""
                    INSERT INTO operator_chat_messages (chat_id, provider_message_id, sender_member_id,
                                                        from_operator, sent_at, body, is_event, raw)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (chat_id, provider_message_id) DO NOTHING""",
                    (chat_id, m["id"], m.get("sender_id"), bool(m.get("is_sender")), m["timestamp"],
                     m.get("text"), bool(m.get("is_event")), json.dumps(m)))
                new += cur.rowcount
                oldest = min(oldest, m["timestamp"]) if oldest else m["timestamp"]
            conn.commit()
            n["stored"] += new
            # A chat whose history is complete only needs the new messages: stop at the first
            # page that added nothing. Without a backfill, the first sync keeps one page.
            if (complete and new == 0) or (not complete and op["backfill"] == "from_first_sync" and first_page):
                reached_end = complete or op["backfill"] == "from_first_sync"
                break
            first_page = False
        cur.execute("""UPDATE operator_chats SET messages_synced_at = now(),
                            oldest_message_at = LEAST(oldest_message_at, %s::timestamptz),
                            history_complete = history_complete OR %s
                        WHERE id = %s""", (oldest, reached_end, chat_id))
        conn.commit()
        n["chats"] += 1
        if i % 50 == 0:
            print(f"    messages: {i}/{len(todo)} chats, {n['stored']} stored", flush=True)
    return dict(n)


def run(client: str, only: set[str], max_chats: int | None) -> int:
    env = load_client_env(client)
    conn = connect(client)
    ops = operators(conn.cursor())
    if not ops:
        print(f"{client}: no active messaging operator with an operator_sync_policy row; nothing to sync")
        return 0
    u = Unipile(env)
    for op in ops:
        print(f"{client} / {op['name']} ({op['channel']}): direct={op['store_direct']} "
              f"groups={op['store_groups']} backfill={op['backfill']}", flush=True)
        if "chats" in only:
            print("  chats:", sync_chats(conn, u, op), flush=True)
        if "members" in only:
            print("  members:", sync_members(conn, u, op, max_chats), flush=True)
        if "messages" in only:
            print("  messages:", sync_messages(conn, u, op, max_chats), flush=True)
    conn.close()
    print(f"{client}: {u.calls} Unipile calls")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--client")
    g.add_argument("--all", action="store_true", help="every discovered instance")
    ap.add_argument("--only", default="chats,members,messages")
    ap.add_argument("--max-chats", type=int, help="cap chats per stage (a smoke test)")
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",")}
    failed = 0
    for client in ([a.client] if a.client else discover_instances()):
        try:
            run(client, only, a.max_chats)
        except Exception as e:  # noqa: BLE001  one instance must not stop the rest
            failed += 1
            print(f"{client}: FAILED {type(e).__name__}: {e}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
