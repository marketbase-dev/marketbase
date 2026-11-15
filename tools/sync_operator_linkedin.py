#!/usr/bin/env python3
"""sync_operator_linkedin.py — an operator's own LinkedIn relationships into MarketBase, from Unipile.

For one operator (an `outbound_operators` row with a Unipile account, e.g. the founder), pull:

  dms       every LinkedIn chat and its messages -> lead_conversations / lead_messages / reactions
            (the same tables sync_conversations.py fills; no they:replied tags: these are personal
            chats, not campaign replies)
  received  every post the operator wrote, and each post's reactions and comments by others
            -> lead_touches, kind 'linkedin', direction 'inbound', disposition 'reaction' | 'comment'
  made      every reaction and comment the operator left on other people's posts
            -> lead_touches, kind 'linkedin', direction 'outbound', disposition 'reaction' | 'comment'
  status    counts

Only people already in the instance's `leads` are written (matched by LinkedIn member id); nobody is
created from a chat or a like. Everyone else stays in the cached pages (enrichment_calls,
api='unipile'), so a later run after new leads are added picks them up without re-fetching.

Every Unipile page is cached before it is processed; a run that dies resumes from the cache.
`--refresh` re-fetches the listing pages (chats, posts, the operator's reactions and comments) to see
new activity; per-chat messages and per-post reactions are re-fetched only where the listing shows
something newer than what is stored.

A reaction carries no time on LinkedIn, so its touched_at is the post's time (raw.date_basis says so).

  python3 sync_operator_linkedin.py dms --client "Northwind"

Unipile credentials come from the environment: UNIPILE_BASE_URL (or UNIPILE_BASE_SERVER) and
UNIPILE_API_KEY (or UNIPILE_ACCESS_TOKEN).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect  # noqa: E402
import api_cache  # noqa: E402
import sync_conversations as SC  # noqa: E402

TOOL = "sync_operator_linkedin.py"
API = "unipile"


class Unipile:
    def __init__(self):
        b = (os.environ.get("UNIPILE_BASE_URL") or os.environ.get("UNIPILE_BASE_SERVER") or "").rstrip("/")
        self.key = os.environ.get("UNIPILE_API_KEY") or os.environ.get("UNIPILE_ACCESS_TOKEN")
        if not b or not self.key:
            sys.exit("Unipile base URL / API key not in the environment")
        self.base = b if b.startswith("http") else "https://" + b

    def get(self, path: str, **q) -> dict:
        url = self.base + path + ("?" + urllib.parse.urlencode({k: v for k, v in q.items() if v is not None}) if q else "")
        for attempt in range(6):
            try:
                req = urllib.request.Request(url, headers={"X-API-KEY": self.key, "accept": "application/json"})
                return json.load(urllib.request.urlopen(req, timeout=60))
            except urllib.error.HTTPError as e:
                body = e.read()[:300].decode("utf8", "replace")
                if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                    time.sleep(20 * (attempt + 1))
                    continue
                return {"_error": e.code, "body": body}
            except Exception as e:                           # noqa: BLE001
                if attempt < 5:
                    time.sleep(10 * (attempt + 1))
                    continue
                return {"_error": "network", "body": str(e)[:200]}
        return {"_error": "retries"}


def ok(d) -> bool:
    return isinstance(d, dict) and isinstance(d.get("items"), list)


def pages(conn, uni, endpoint: str, path: str, base_params: dict, *, fresh: bool, limit: int = 100, send=("account_id",)):
    """Yield every page of a cursor-paged Unipile listing, cached per page. `base_params` is the cache
    key; only the names in `send` (plus limit and cursor) go to Unipile, the rest identify the path."""
    cursor = None
    while True:
        params = dict(base_params, limit=limit, cursor=cursor)
        key = {k: v for k, v in params.items() if v is not None}
        req = {k: v for k, v in params.items() if k in send or k in ("limit", "cursor")}
        last = {}

        def fetch(p=req):
            d = uni.get(path, **p)
            last["d"] = d
            return d if ok(d) else None                      # a failure is never cached

        data, _ = api_cache.cached_call(conn, API, endpoint, key, fetch, use_cache=not fresh, cost=0)
        if not ok(data):
            raise RuntimeError(f"{endpoint} {key}: {json.dumps(last.get('d'))[:300]}")
        yield data
        cursor = data.get("cursor") or (data.get("paging") or {}).get("cursor")
        if not cursor or not data["items"]:
            return


def operator(conn, name: str | None) -> dict:
    cur = conn.cursor()
    cur.execute("""SELECT id::text, display_name, channel_identifier, unipile_account_id FROM outbound_operators
                    WHERE is_active AND channel = 'linkedin' AND unipile_account_id IS NOT NULL
                      AND (%s::text IS NULL OR display_name = %s)""", (name, name))
    rows = cur.fetchall()
    conn.rollback()
    if len(rows) != 1:
        sys.exit(f"expected exactly one active LinkedIn operator, found {len(rows)}; pass --operator \"<display name>\"")
    return dict(zip(("id", "name", "member_id", "account_id"), rows[0]))


def lead_index(conn) -> dict[str, str]:
    """LinkedIn member id (ACoAA...) -> live lead id."""
    cur = conn.cursor()
    cur.execute("""SELECT i.value, COALESCE(l.merged_into, l.id)::text FROM lead_identities i JOIN leads l ON l.id = i.lead_id
                    WHERE i.kind IN ('linkedin_urn', 'linkedin_urn_search')""")
    idx = dict(cur.fetchall())
    cur.execute("SELECT member_urn, id::text FROM leads WHERE member_urn IS NOT NULL AND merged_into IS NULL")
    for urn, lid in cur.fetchall():
        idx.setdefault(urn, lid)
    conn.rollback()
    return idx


def ts(v: str | None):
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def activity_time(post_id: str | None):
    """A LinkedIn activity id carries its creation time in its top 41 bits."""
    digits = "".join(ch for ch in (post_id or "").split(":")[-1] if ch.isdigit())
    if len(digits) < 18:
        return None
    return datetime.fromtimestamp((int(digits) >> 22) / 1000, tz=timezone.utc)


def touch(cur, lead_id, when, op, direction, disposition, subject, source, external_id, raw) -> int:
    cur.execute("""INSERT INTO lead_touches (lead_id, touched_at, rep, rep_role, kind, direction, disposition,
                                             subject, source, external_id, raw)
                   VALUES (%s, %s, %s, 'operator', 'linkedin', %s, %s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (source, external_id) WHERE external_id IS NOT NULL DO NOTHING""",
                (lead_id, when, op["name"], direction, disposition, subject, source, external_id, json.dumps(raw, default=str)))
    return cur.rowcount


# -- dms ----------------------------------------------------------------------------

def stage_dms(a, conn, uni, op) -> int:
    idx = lead_index(conn)
    cur = conn.cursor()
    # last_synced_at, not last_message_at: a chat's timestamp also moves on reactions and edits, so comparing
    # it with the newest message re-read every chat on every run (2026-11-14).
    cur.execute("SELECT lead_id::text, last_synced_at FROM lead_conversations WHERE operator_id = %s AND channel = 'linkedin'", (op["id"],))
    have = dict(cur.fetchall())
    conn.rollback()
    t = collections.Counter()
    for page in pages(conn, uni, "/api/v1/chats", "/api/v1/chats", {"account_id": op["account_id"]}, fresh=a.refresh, limit=250):
        for chat in page["items"]:
            t["chats"] += 1
            pid = chat.get("attendee_provider_id")
            if chat.get("type") != 0 or not pid:
                t["group or organisation chats (skipped)"] += 1
                continue
            lead_id = idx.get(pid)
            if not lead_id:
                t["chats with people not in leads"] += 1
                continue
            last = ts(chat.get("timestamp"))
            if lead_id in have and have[lead_id] and last and have[lead_id] >= last:
                t["up to date"] += 1
                continue
            msgs = []
            for mp in pages(conn, uni, "/api/v1/chats/messages", f"/api/v1/chats/{chat['id']}/messages",
                            {"chat_id": chat["id"]}, fresh=True, limit=250):
                msgs.extend(mp["items"])
            conv = SC.upsert_conversation(cur, op["id"], lead_id, "linkedin", chat)
            for m in msgs:
                if not m.get("provider_id") or not m.get("timestamp"):
                    continue
                mid, new = SC.upsert_message(cur, conv, m)
                SC.upsert_reactions(cur, mid, m)
                t["messages new"] += new
                t["messages"] += 1
            SC.recompute_conversation_stats(cur, conv)
            conn.commit()                                    # per conversation: a crash keeps what is done
            t["conversations synced"] += 1
            if t["conversations synced"] % 100 == 0:
                print(dict(t), flush=True)
    print(dict(t))
    return 0


# -- received -----------------------------------------------------------------------

def stage_received(a, conn, uni, op) -> int:
    idx = lead_index(conn)
    cur = conn.cursor()
    t = collections.Counter()
    for page in pages(conn, uni, "/api/v1/users/posts", f"/api/v1/users/{op['member_id']}/posts",
                      {"account_id": op["account_id"], "user": op["member_id"]}, fresh=a.refresh):
        for post in page["items"]:
            t["posts"] += 1
            sid = post.get("social_id")
            when = ts(post.get("parsed_datetime")) or activity_time(sid)
            if not sid or not when:
                continue
            path = "/api/v1/posts/" + urllib.parse.quote(sid, safe="")
            subject = " ".join((post.get("text") or "").split())[:120] or None
            # A post's engagement is re-read only when --refresh is on and the post is recent.
            fresh = a.refresh and (datetime.now(timezone.utc) - when).days <= a.recent_days
            if post.get("reaction_counter"):
                for rp in pages(conn, uni, "/api/v1/posts/reactions", path + "/reactions",
                                {"account_id": op["account_id"], "post": sid}, fresh=fresh):
                    for r in rp["items"]:
                        au = r.get("author") or {}
                        t["reactions"] += 1
                        lead_id = idx.get(au.get("id"))
                        if not lead_id or au.get("id") == op["member_id"]:
                            t["reactions by people not in leads"] += not lead_id
                            continue
                        t["reaction touches added"] += touch(
                            cur, lead_id, when, op, "inbound", "reaction", subject, "unipile:linkedin_post_reaction",
                            f"{sid}|{au['id']}", {"post": sid, "post_url": post.get("share_url"), "value": r.get("value"),
                                                  "author": au, "date_basis": "post time", "_source": TOOL})
            if post.get("comment_counter"):
                for cp in pages(conn, uni, "/api/v1/posts/comments", path + "/comments",
                                {"account_id": op["account_id"], "post": sid}, fresh=fresh):
                    for c in cp["items"]:
                        au = c.get("author_details") or {}
                        t["comments"] += 1
                        lead_id = idx.get(au.get("id"))
                        if au.get("id") == op["member_id"] or not lead_id:
                            t["comments by people not in leads"] += (not lead_id and au.get("id") != op["member_id"])
                            continue
                        t["comment touches added"] += touch(
                            cur, lead_id, ts(c.get("date")) or when, op, "inbound", "comment", subject,
                            "unipile:linkedin_post_comment", str(c.get("id")),
                            {"post": sid, "post_url": post.get("share_url"), "comment": c, "_source": TOOL})
            conn.commit()
            if t["posts"] % 50 == 0:
                print(dict(t), flush=True)
    print(dict(t))
    return 0


# -- made ---------------------------------------------------------------------------

def post_author(conn, uni, op, post_id: str) -> dict:
    urn = post_id if post_id.startswith("urn:") else f"urn:li:activity:{post_id}"
    def fetch():
        d = uni.get("/api/v1/posts/" + urllib.parse.quote(urn, safe=""), account_id=op["account_id"])
        if isinstance(d, dict) and "author" in d:
            return d
        if isinstance(d, dict) and d.get("_error") in (404, 422):
            return {"unreadable": d["_error"]}               # a deleted or private post: cached, never retried
        return None                                          # anything else is retried next run

    data, _ = api_cache.cached_call(conn, API, "/api/v1/posts", {"post": urn}, fetch, cost=0)
    return data if isinstance(data, dict) else {"unreadable": "fetch failed"}


def stage_made(a, conn, uni, op) -> int:
    idx = lead_index(conn)
    cur = conn.cursor()
    t = collections.Counter()
    for kind in ("reactions", "comments"):
        for page in pages(conn, uni, f"/api/v1/users/{kind}", f"/api/v1/users/{op['member_id']}/{kind}",
                          {"account_id": op["account_id"], "user": op["member_id"]}, fresh=a.refresh):
            for it in page["items"]:
                t[kind] += 1
                pid = str(it.get("post_id") or "")
                if not pid:
                    continue
                post = post_author(conn, uni, op, pid)
                au = post.get("author") or {}
                author_id = au.get("id") or au.get("public_identifier")
                if not post or post.get("unreadable") or post.get("_error"):
                    t["post not readable"] += 1
                    continue
                if author_id == op["member_id"]:
                    t["on the operator's own posts"] += 1
                    continue
                lead_id = idx.get(author_id)
                if not lead_id:
                    t[f"{kind} on posts by people not in leads"] += 1
                    continue
                subject = " ".join((post.get("text") or "").split())[:120] or None
                if kind == "reactions":
                    t["reaction touches added"] += touch(
                        cur, lead_id, ts(post.get("parsed_datetime")) or activity_time(pid), op, "outbound", "reaction", subject,
                        "unipile:linkedin_reaction_made", f"{pid}|{op['member_id']}",
                        {"post": pid, "post_url": post.get("share_url"), "value": it.get("value"), "post_author": au,
                         "date_basis": "post time", "_source": TOOL})
                else:
                    t["comment touches added"] += touch(
                        cur, lead_id, ts(it.get("date")) or activity_time(pid), op, "outbound", "comment", subject,
                        "unipile:linkedin_comment_made", str(it.get("id")),
                        {"post": pid, "post_url": post.get("share_url"), "comment": it, "post_author": au, "_source": TOOL})
            conn.commit()
            print(dict(t), flush=True)
    print(dict(t))
    return 0


def stage_status(a, conn, uni, op) -> int:
    cur = conn.cursor()
    cur.execute("""SELECT count(*), count(*) FILTER (WHERE message_count > 0), sum(message_count) FROM lead_conversations
                    WHERE operator_id = %s""", (op["id"],))
    print("conversations, with messages, messages:", cur.fetchone())
    cur.execute("""SELECT source, direction, count(*), count(DISTINCT lead_id) FROM lead_touches WHERE rep = %s
                    GROUP BY 1, 2 ORDER BY 1""", (op["name"],))
    for r in cur.fetchall():
        print(" ", r)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("stage", choices=("dms", "received", "made", "status"))
    ap.add_argument("--client", required=True)
    ap.add_argument("--operator", default=None, help="outbound_operators.display_name; needed when there is more than one")
    ap.add_argument("--refresh", action="store_true", help="re-fetch the listing pages to see new activity")
    ap.add_argument("--recent-days", type=int, default=45, help="with --refresh: posts this recent get their engagement re-read")
    a = ap.parse_args()
    conn = connect(a.client)
    op = operator(conn, a.operator)
    uni = Unipile() if a.stage != "status" else None
    return {"dms": stage_dms, "received": stage_received, "made": stage_made, "status": stage_status}[a.stage](a, conn, uni, op)


if __name__ == "__main__":
    sys.exit(main())
