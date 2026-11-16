#!/usr/bin/env python3
"""sync_collectives.py — groups, communities and events, and who belongs to them (schema 096).

Reads what the instance already holds and files it as collectives and memberships. It calls no
provider. Every stage can be re-run: collectives and memberships are upserted, and a status that
changed since the last run lands in collective_membership_changes through the table's trigger.

  events         every event-guest row in lead_sources (Luma, Partiful) becomes a membership of
                 that event: the application outcome (applied, approved, declined, waitlisted,
                 invited), attendance, and the source's own wording. A guest's phone number is
                 filed as a `phone` identity, so the same person in a messaging group is found
                 by number instead of becoming a second person.
  chats          every messaging group an operator can see (operator_chats, 094) becomes a
                 collective, and EVERY chat member, of groups and of direct chats, becomes a
                 person: found by member id or number when already on file, else created with
                 the name the network shows. Group members get a membership (member | left).
  join-requests  lead_sources rows of a group join request become `applied` memberships of the
                 group they name, when exactly one group has that name.
  tag            --tag T --name N [--kind community] [--attended]: everyone carrying tag T
                 becomes a member of a collective named N (for a tag that stood in for a group).
  retire-tag     --tag T --message M: mark the tag retired. Writing it then fails with M, and so
                 does any reader that wraps the name in live_tag().
  propose        propose communities: a community a network reports as two same-name groups, and
                 collectives whose names start with the same two words. Marked `inferred`; never
                 replaces a decided link.
  decide         --id C --by WHO with --parent ID | --new-parent NAME | --no-parent, --company-id ID |
                 --no-company, --set-kind: one person's decision about one collective.
  match          "maybe the same person": two different people on file with the same full name in
                 the same community, group or event. Filed in lead_possible_matches with the name
                 and the shared context as evidence. Nobody is merged on it; a pair turns `proven`
                 by itself once an exact identifier joins the two.
  daily          chats, join-requests, match, in that order (the run after a chat sync).
                 With --all instead of --client: every instance.
  report         counts.

People are matched on exact identities only (member id, number, e-mail, profile URL). Identities
are written through lead_identity_claim(), so a value another person already holds queues the
pair for merge_lead() instead of being reassigned.

Usage:
  python3 sync_collectives.py events --client <Name>
  python3 sync_collectives.py chats --client <Name>
  python3 sync_collectives.py retire-tag --client <Name> --tag audience:x --message "..."
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import unicodedata
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, discover_instances  # noqa: E402

ME = "sync_collectives.py"
BATCH = 500
PHONE_NAME = re.compile(r"^\+?[\d\s()\-.]{6,}$")
PRIORITY = {"email": 0, "linkedin_url": 1, "phone": 2, "whatsapp": 3, "linkedin_urn": 4}

# How each guest-list source words its statuses: (provider, event id key, status key, status map,
# check-in test, check-in time key, phone key).
EVENT_SOURCES = {
    "luma_event_guest": dict(
        provider="luma", event_key="event_api_id", status_key="approval_status",
        status={"approved": "approved", "pending_approval": "applied", "declined": "declined",
                "waitlist": "waitlisted", "invited": "invited"},
        attended=lambda r: bool(r.get("checked_in_at")), attended_at="checked_in_at", phone="phone_number"),
    "partiful_event_guest": dict(
        provider="partiful", event_key="event_id", status_key="Status",
        status={"approved": "approved", "pending": "applied", "can't go": "declined", "waitlist": "waitlisted",
                "invited": "invited"},
        attended=lambda r: str(r.get("Checked in") or "").strip().lower() == "yes", attended_at="Check-in Time",
        phone="What is your phone number?"),
}


def phone(v) -> str | None:
    d = re.sub(r"\D", "", str(v or ""))
    return f"+{d}" if 7 <= len(d) <= 15 else None


def chunks(seq, n=BATCH):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def claim(cur, rows: list[tuple], source: str) -> collections.Counter:
    """rows: (lead id, kind, value). Returns the outcomes of lead_identity_claim()."""
    out = collections.Counter()
    for part in chunks(rows):
        cur.execute("""SELECT split_part(lead_identity_claim(l::uuid, k, v, %s, false, NULL), ':', 1)
                         FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(l, k, v)""",
                    (source, [r[0] for r in part], [r[1] for r in part], [r[2] for r in part]))
        out.update(r[0] for r in cur.fetchall())
    return out


def collective(cur, kind, name, provider, provider_id, source, operator_chat_id=None, raw=None) -> str:
    cur.execute("""
        INSERT INTO collectives (kind, name, provider, provider_id, operator_chat_id, source, raw)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (provider, provider_id) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, collectives.name), last_seen_at = now(),
            operator_chat_id = COALESCE(EXCLUDED.operator_chat_id, collectives.operator_chat_id)
        RETURNING id::text""", (kind, name, provider, provider_id, operator_chat_id, source,
                                json.dumps(raw) if raw is not None else None))
    return cur.fetchone()[0]


def upsert_memberships(cur, rows: list[dict]) -> int:
    """rows: collective_id, kind, value, role, application, attended, attended_at, member_state,
    source_status, source, source_ref, first_seen_at. One statement per batch; the trigger logs changes."""
    n = 0
    for part in chunks(rows):
        cur.execute("""
            INSERT INTO collective_memberships (collective_id, member_kind, member_value, role, application, attended,
                                                attended_at, member_state, source_status, source, source_ref, first_seen_at)
            SELECT (r->>'collective_id')::uuid, r->>'kind', r->>'value', COALESCE(r->>'role', 'member'), r->>'application',
                   (r->>'attended')::boolean, NULLIF(r->>'attended_at', '')::timestamptz, r->>'member_state',
                   r->>'source_status', r->>'source', r->>'source_ref',
                   COALESCE(NULLIF(r->>'first_seen_at', '')::timestamptz, now())
              FROM jsonb_array_elements(%s::jsonb) r
            ON CONFLICT (collective_id, member_kind, member_value) DO UPDATE SET
                role = EXCLUDED.role, application = COALESCE(EXCLUDED.application, collective_memberships.application),
                attended = COALESCE(EXCLUDED.attended, collective_memberships.attended),
                attended_at = COALESCE(EXCLUDED.attended_at, collective_memberships.attended_at),
                member_state = COALESCE(EXCLUDED.member_state, collective_memberships.member_state),
                source_status = COALESCE(EXCLUDED.source_status, collective_memberships.source_status),
                source = EXCLUDED.source, source_ref = EXCLUDED.source_ref, last_seen_at = now()""",
                    (json.dumps(part, default=str),))
        n += cur.rowcount
    return n


def member_identity(cur, lead_ids: list[str]) -> dict[str, tuple[str, str]]:
    """The identity a membership is keyed by, per person: the best one on file."""
    best: dict[str, tuple] = {}
    for part in chunks(lead_ids, 2000):
        cur.execute("SELECT lead_id::text, kind, value FROM lead_identities WHERE lead_id::text = ANY(%s)", (part,))
        for lid, kind, value in cur.fetchall():
            rank = (PRIORITY.get(kind, 9), value)
            if lid not in best or rank < best[lid][0]:
                best[lid] = (rank, kind, value)
    return {lid: (b[1], b[2]) for lid, b in best.items()}


def existing_keys(cur) -> dict[tuple, tuple]:
    """(collective, person) -> the identity an earlier run keyed that membership by, so a person who
    gained a better identity since does not get a second membership."""
    cur.execute("""SELECT collective_id::text, lead_id::text, member_kind, member_value FROM v_collective_members
                    WHERE lead_id IS NOT NULL""")
    return {(c, l): (k, v) for c, l, k, v in cur.fetchall()}


# ── events ─────────────────────────────────────────────────────────────────────
def stage_events(conn, a) -> int:
    cur = conn.cursor()
    cur.execute("""SELECT s.id::text, s.lead_id::text, s.source_type, s.source_label, s.raw_data, s.recorded_at
                     FROM lead_sources s JOIN leads l ON l.id = s.lead_id
                    WHERE s.source_type = ANY(%s) AND l.merged_into IS NULL
                    ORDER BY s.recorded_at""", (list(EVENT_SOURCES),))
    rows = cur.fetchall()
    ident = member_identity(cur, sorted({r[1] for r in rows}))
    known = existing_keys(cur)
    n = collections.Counter()
    events: dict[tuple, str] = {}
    members: dict[tuple, dict] = {}
    phones = []
    for sid, lead, stype, label, raw, recorded in rows:
        cfg = EVENT_SOURCES[stype]
        eid = raw.get(cfg["event_key"])
        if not eid:
            n["no event id"] += 1
            continue
        key = (cfg["provider"], eid)
        if key not in events:
            name = re.sub(r"\s*\((Luma|Partiful) [^)]*\)\s*$", "", label or "").strip() or None
            events[key] = collective(cur, "event", name, cfg["provider"], eid, f"{ME} events ({stype})")
        if lead not in ident:
            n["guest with no identity on file"] += 1
            continue
        cid = events[key]
        kind, value = known.get((cid, lead)) or ident[lead]
        status = str(raw.get(cfg["status_key"]) or "").replace("\xa0", " ").strip()
        attended = cfg["attended"](raw)
        # later rows of the same guest replace earlier ones: the list is ordered by recorded_at
        members[(cid, lead)] = dict(
            collective_id=cid, kind=kind, value=value, role="guest",
            application=cfg["status"].get(status.lower()), attended=attended if attended else None,
            attended_at=raw.get(cfg["attended_at"]) if attended else None, source_status=status or None,
            source=f"lead_sources:{stype}", source_ref=sid, first_seen_at=recorded)
        if status and status.lower() not in cfg["status"]:
            n[f"status not mapped: {status}"] += 1
        p = phone(raw.get(cfg["phone"]))
        if p:
            phones.append((lead, "phone", p))
    n["events"] = len(events)
    n["memberships written"] = upsert_memberships(cur, list(members.values()))
    for k, v in claim(cur, sorted(set(phones)), f"{ME} events").items():
        n[f"phone {k}"] += v
    conn.commit()
    print("events:", dict(n))
    return 0


# ── chats ──────────────────────────────────────────────────────────────────────
def stage_chats(conn, a) -> int:
    cur = conn.cursor()
    n = collections.Counter()
    cur.execute("""INSERT INTO source_types (name, description, purpose, created_by)
                   VALUES ('operator_chat_member', 'A person seen in an operator''s own messaging chats: a member of '
                           'one of the operator''s groups, or the other side of a direct chat (094).',
                           'People the operator knows through a messaging network.', %s)
                   ON CONFLICT (name) DO NOTHING""", (ME,))
    cur.execute("""SELECT c.id::text, c.provider_chat_id, c.provider_thread_id, c.kind, c.title, o.channel, o.display_name
                     FROM operator_chats c JOIN outbound_operators o ON o.id = c.operator_id""")
    chats = {r[0]: r for r in cur.fetchall()}
    coll = {}
    for chat_id, pcid, thread, kind, title, channel, op_name in chats.values():
        if kind == "group":
            coll[chat_id] = collective(cur, "group", title, channel, thread or pcid, f"{ME} chats", operator_chat_id=chat_id)
    n["group collectives"] = len(coll)
    conn.commit()

    cur.execute("""SELECT m.chat_id::text, m.provider_member_id, m.phone, m.display_name, m.left_at, m.first_seen_at
                     FROM operator_chat_members m WHERE NOT m.is_self""")
    rows = cur.fetchall()
    people: dict[str, dict] = {}
    for chat_id, mid, ph, name, left, first in rows:
        p = people.setdefault(mid, {"phone": None, "names": collections.Counter(), "chats": set()})
        p["phone"] = p["phone"] or phone(ph)
        if name and name.strip() and not PHONE_NAME.match(name.strip()):
            p["names"][name.strip()] += 1
        p["chats"].add(chat_id)
    lid_kind = "whatsapp_lid"
    cur.execute("SELECT kind, value, lead_id::text FROM lead_identities WHERE kind IN (%s, 'whatsapp', 'phone')", (lid_kind,))
    owner = {(k, v): l for k, v, l in cur.fetchall()}

    new_leads, claims, sources = [], [], []
    for mid, p in people.items():
        lead = owner.get((lid_kind, mid)) or (p["phone"] and (owner.get(("whatsapp", p["phone"]))
                                                               or owner.get(("phone", p["phone"]))))
        if lead:
            n["people already on file"] += 1
        else:
            lead = str(uuid.uuid4())
            name = p["names"].most_common(1)[0][0] if p["names"] else None
            new_leads.append((lead, name))
            op = chats[next(iter(p["chats"]))]
            sources.append((lead, f"{op[6]} {op[5]} chats", json.dumps(
                {"_source": ME, "member_id": mid, "phone": p["phone"], "chats": len(p["chats"])})))
        if (lid_kind, mid) not in owner:
            claims.append((lead, lid_kind, mid))
        if p["phone"] and ("whatsapp", p["phone"]) not in owner:
            claims.append((lead, "whatsapp", p["phone"]))
        p["lead"] = lead
    for part in chunks(new_leads):
        cur.execute("""INSERT INTO leads (id, name) SELECT l::uuid, n FROM unnest(%s::text[], %s::text[]) AS t(l, n)""",
                    ([x[0] for x in part], [x[1] for x in part]))
        n["people created"] += cur.rowcount
        conn.commit()
    for part in chunks(sources):
        cur.execute("""INSERT INTO lead_sources (lead_id, source_type, source_label, source_date, raw_data)
                       SELECT l::uuid, 'operator_chat_member', s, current_date, r::jsonb
                         FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(l, s, r)""",
                    ([x[0] for x in part], [x[1] for x in part], [x[2] for x in part]))
        conn.commit()
    for part in chunks(claims, 2000):
        for k, v in claim(cur, part, f"{ME} chats").items():
            n[f"identity {k}"] += v
        conn.commit()

    members = [dict(collective_id=coll[chat_id], kind=lid_kind, value=mid, role="member",
                    member_state="left" if left else "member", source="operator_chat_members",
                    source_ref=chat_id, first_seen_at=first)
               for chat_id, mid, ph, name, left, first in rows if chat_id in coll]
    for part in chunks(members, 2000):
        n["memberships written"] += upsert_memberships(cur, part)
        conn.commit()
    print("chats:", dict(n))
    return 0


# ── join requests ──────────────────────────────────────────────────────────────
def stage_join_requests(conn, a) -> int:
    cur = conn.cursor()
    cur.execute("""SELECT s.id::text, s.lead_id::text, s.raw_data FROM lead_sources s JOIN leads l ON l.id = s.lead_id
                    WHERE s.source_type LIKE '%%group_join_request' AND l.merged_into IS NULL""")
    rows = cur.fetchall()
    cur.execute("SELECT name, array_agg(id::text) FROM collectives WHERE kind IN ('group', 'community') GROUP BY 1")
    by_name = dict(cur.fetchall())
    n = collections.Counter()
    members, phones = [], []
    for sid, lead, raw in rows:
        ids = by_name.get(raw.get("group")) or []
        p = phone(raw.get("phone"))
        if len(ids) != 1 or not p:
            n[f"skipped: {len(ids)} groups named {raw.get('group')!r}" if p else "skipped: no number"] += 1
            continue
        members.append(dict(collective_id=ids[0], kind="whatsapp", value=p, role="member", application="applied",
                            source_status=f"join request ({raw.get('join_via') or 'unknown'})",
                            source="lead_sources:group_join_request", source_ref=sid,
                            first_seen_at=raw.get("requested_at")))
        phones.append((lead, "whatsapp", p))
    for k, v in claim(cur, phones, f"{ME} join-requests").items():
        n[f"identity {k}"] += v
    n["memberships written"] = upsert_memberships(cur, members)
    conn.commit()
    print("join-requests:", dict(n))
    return 0


# ── a tag that stood in for a group ────────────────────────────────────────────
def stage_tag(conn, a) -> int:
    cur = conn.cursor()
    cur.execute("""SELECT t.lead_id::text, t.id::text, t.tagged_at FROM lead_tags t JOIN leads l ON l.id = t.lead_id
                    WHERE t.tag = %s AND l.merged_into IS NULL""", (a.tag,))
    rows = cur.fetchall()
    if not rows:
        sys.exit(f"nobody carries {a.tag}")
    cid = collective(cur, a.kind, a.name, "tag", a.tag, f"{ME} tag")
    ident = member_identity(cur, [r[0] for r in rows])
    known = existing_keys(cur)
    members = []
    for lead, tid, at in rows:
        if lead not in ident:
            continue
        kind, value = known.get((cid, lead)) or ident[lead]
        members.append(dict(collective_id=cid, kind=kind, value=value, role="member",
                            attended=True if a.attended else None, source=f"lead_tags:{a.tag}", source_ref=tid,
                            first_seen_at=at))
    n = upsert_memberships(cur, members)
    conn.commit()
    print(f"tag: {a.tag} -> {a.name!r} ({a.kind}): {len(rows)} carriers, {n} memberships written, "
          f"{len(rows) - len(members)} without an identity")
    return 0


def stage_retire_tag(conn, a) -> int:
    ns, _, key = a.tag.partition(":")
    cur = conn.cursor()
    cur.execute("SELECT entity_type FROM tag_definitions WHERE namespace = %s AND key = %s", (ns, key))
    found = cur.fetchall()
    if len(found) != 1:
        sys.exit(f"{a.tag}: {len(found)} definitions, expected exactly 1")
    cur.execute("""UPDATE tag_definitions SET retired_message = %s, deprecated_by = COALESCE(deprecated_by, %s)
                    WHERE namespace = %s AND key = %s AND entity_type = %s""",
                (a.message, a.replaced_by or "collective_memberships (096)", ns, key, found[0][0]))
    if cur.rowcount != 1:
        conn.rollback()
        sys.exit(f"expected 1 row, got {cur.rowcount}")
    conn.commit()
    print(f"retired {a.tag}: writing it now fails with: {a.message}")
    return 0


# ── communities ────────────────────────────────────────────────────────────────
def name_key(name: str | None) -> list[str]:
    """Words of a name for grouping: lower case, punctuation as spaces, a bracketed letter joined
    to its word ("(N)YC" reads as "nyc")."""
    s = re.sub(r"\((\w)\)", r"\1", (name or "").lower())
    return re.findall(r"[^\W_]+", s)       # letters and digits of any script


def community(cur, name: str, source: str) -> str:
    slug = "-".join(name_key(name))
    return collective(cur, "community", name, "manual", f"community:{slug}", source)


def stage_propose(conn, a) -> int:
    """Propose which collectives belong together. Two inferences, both marked `inferred`, neither ever
    replacing a link someone decided:
      twins  a messaging network reports a community as two groups with one name: the community itself,
             whose members only its admins can see (5 or fewer listed), and its announcement group, which
             lists everyone (20 or more). The first becomes kind community and the parent of the second.
      stems  collectives whose names start with the same two words share a community of that name."""
    cur = conn.cursor()
    cur.execute("""SELECT c.id::text, c.kind, c.name, c.provider, c.parent_id::text, c.parent_source,
                          (SELECT count(*) FROM collective_memberships m WHERE m.collective_id = c.id)
                     FROM collectives c""")
    rows = {r[0]: dict(id=r[0], kind=r[1], name=r[2], provider=r[3], parent=r[4], psrc=r[5], n=r[6]) for r in cur.fetchall()}
    n = collections.Counter()

    def link(child: str, parent: str):
        c = rows[child]
        if child == parent or c["psrc"] == "decided" or c["parent"] == parent:
            return
        cur.execute("""UPDATE collectives SET parent_id = %s, parent_source = 'inferred', parent_decided_by = %s,
                              parent_decided_at = now()
                        WHERE id = %s AND parent_source IS DISTINCT FROM 'decided'""", (parent, f"{ME} propose", child))
        n["links proposed"] += cur.rowcount
        c["parent"], c["psrc"] = parent, "inferred"

    twins = collections.defaultdict(list)
    for c in rows.values():
        if c["kind"] in ("group", "community") and c["provider"] not in ("manual", "tag") and name_key(c["name"]):
            twins[(c["provider"], " ".join(name_key(c["name"])))].append(c)
    roots: dict[str, str] = {}
    for (_, key), cs in twins.items():
        big = max(cs, key=lambda c: c["n"])
        small = sorted((c for c in cs if c is not big and c["n"] <= 5), key=lambda c: c["id"])
        if len(cs) < 2 or big["n"] < 20 or not small:
            continue
        head = small[0]
        if head["kind"] != "community":
            cur.execute("UPDATE collectives SET kind = 'community' WHERE id = %s AND kind = 'group'", (head["id"],))
            n["communities found as twins"] += cur.rowcount
            head["kind"] = "community"
        roots[key] = head["id"]
        for c in cs:
            link(c["id"], head["id"])

    stems = collections.defaultdict(list)
    for c in rows.values():
        k = name_key(c["name"])
        if len(k) >= 2 and len(k[0]) >= 2 and len(k[1]) >= 2 and c["provider"] != "manual":
            stems[" ".join(k[:2])].append(c)
    for stem, cs in stems.items():
        if len({c["id"] for c in cs}) < 2 or len({" ".join(name_key(c["name"])) for c in cs}) < 2:
            continue
        root = roots.get(stem)
        if not root:
            shortest = min(cs, key=lambda c: len(c["name"]))
            words = re.findall(r"[^\W_]+|\(\w\)\w+", shortest["name"])[:2]
            root = community(cur, " ".join(words), f"{ME} propose")
            rows.setdefault(root, dict(id=root, kind="community", name=" ".join(words), provider="manual",
                                       parent=None, psrc=None, n=0))
            n["communities proposed from names"] += 1
        for c in cs:
            if c["parent"] is None or c["psrc"] == "inferred":
                top = rows.get(c["parent"] or "")
                # a twin's community hangs under the stem community; its announcement group stays under the twin
                link(c["id"] if not top or top["id"] == root else top["id"], root)
    if getattr(a, "monitored_only", False):
        # Keep a proposal only where its community holds a group the operator monitors (094):
        # every other inferred link is withdrawn. Decided links are never touched.
        cur.execute("""
            WITH RECURSIVE up AS (
                SELECT id, id AS root, parent_id FROM collectives
                UNION ALL
                SELECT up.id, c.id, c.parent_id FROM up JOIN collectives c ON c.id = up.parent_id),
            top AS (SELECT id, root FROM up WHERE parent_id IS NULL),
            live AS (SELECT DISTINCT t.root FROM top t JOIN collectives c ON c.id = t.id
                       JOIN v_operator_chat_monitoring m ON m.chat_id = c.operator_chat_id AND m.monitor)
            UPDATE collectives c SET parent_id = NULL, parent_source = NULL, parent_decided_by = %s,
                                     parent_decided_at = now()
              FROM top t
             WHERE t.id = c.id AND c.parent_source = 'inferred' AND t.root NOT IN (SELECT root FROM live)""",
                    (f"{ME} propose --monitored-only (withdrawn)",))
        n["links withdrawn (no monitored group in the community)"] = cur.rowcount
    conn.commit()
    print("propose:", dict(n))
    return 0


def stage_decide(conn, a) -> int:
    """One person's decision about one collective, keyed by id: its community, its company, its kind."""
    cur = conn.cursor()
    cur.execute("SELECT name, kind FROM collectives WHERE id = %s", (a.id,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"no collective {a.id}")
    sets, args = [], []
    if a.parent or a.new_parent or a.no_parent:
        parent = a.parent or (community(cur, a.new_parent, f"{ME} decide ({a.by})") if a.new_parent else None)
        if parent:
            cur.execute("SELECT kind FROM collectives WHERE id = %s", (parent,))
            got = cur.fetchone()
            if not got or got[0] != "community":
                sys.exit(f"parent {parent} is not a community")
        sets += ["parent_id = %s", "parent_source = 'decided'", "parent_decided_by = %s", "parent_decided_at = now()"]
        args += [parent, a.by]
    if a.company_id or a.no_company:
        sets += ["company_id = %s", "company_source = 'decided'", "company_decided_by = %s", "company_decided_at = now()"]
        args += [a.company_id, a.by]
    if a.set_kind:
        sets.append("kind = %s")
        args.append(a.set_kind)
    if not sets:
        sys.exit("decide needs one of --parent, --new-parent, --no-parent, --company-id, --no-company, --set-kind")
    cur.execute(f"UPDATE collectives SET {', '.join(sets)} WHERE id = %s", (*args, a.id))
    if cur.rowcount != 1:
        conn.rollback()
        sys.exit(f"expected 1 row, got {cur.rowcount}")
    conn.commit()
    print(f"decided ({a.by}): {row[0]!r}: {', '.join(s.split(' = ')[0] for s in sets if 'decided' not in s)}")
    return 0


# ── "maybe the same person" ────────────────────────────────────────────────────
MAX_SAME_NAME = 3      # more people than this with one name in one context: the name says nothing


def full_name_key(name: str | None) -> str | None:
    """A full name as a comparison key: the part before any | , ( / @, letters only, accents and
    case folded. None unless at least two words of two letters or more remain."""
    head = re.split(r"[|,(/@]", name or "")[0]
    flat = "".join(ch for ch in unicodedata.normalize("NFKD", head) if not unicodedata.combining(ch)).casefold()
    words = re.findall(r"[^\W\d_]{2,}", flat)
    return " ".join(words) if len(words) >= 2 else None


def stage_match(conn, a) -> int:
    cur = conn.cursor()
    n = collections.Counter()
    # every membership that resolves to a person, with its context: the community, else the collective
    cur.execute("""SELECT v.lead_id::text, l.name, r.id::text, r.name, r.kind, c.name
                     FROM v_collective_members v
                     JOIN collectives c ON c.id = v.collective_id
                     JOIN collectives r ON r.id = COALESCE(c.parent_id, c.id)
                     JOIN leads l ON l.id = v.lead_id AND l.merged_into IS NULL
                    WHERE c.provider <> 'web' AND l.name IS NOT NULL""")
    ctx: dict[tuple, dict[str, set]] = collections.defaultdict(lambda: collections.defaultdict(set))
    shown, roots = {}, {}
    for lead, name, root, root_name, root_kind, coll_name in cur.fetchall():
        key = full_name_key(name)
        if key:
            ctx[(root, key)][lead].add(coll_name or "?")
            shown[lead] = name
            roots[root] = (root_name, root_kind)
    pairs = {}
    for (root, key), people in ctx.items():
        if len(people) < 2:
            continue
        if len(people) > MAX_SAME_NAME:
            n["names skipped (shared by too many people in one context)"] += 1
            continue
        ids = sorted(people)
        for i, x in enumerate(ids):
            for y in ids[i + 1:]:
                pairs.setdefault((x, y), (root, key, people))    # first context found is the evidence
    n["pairs found"] = len(pairs)

    cur.execute("SELECT id::text, a_lead_id::text, b_lead_id::text FROM v_lead_possible_matches")
    on_file = {frozenset((x, y)): pid for pid, x, y in cur.fetchall() if x and y and x != y}
    seen = [on_file[frozenset(p)] for p in pairs if frozenset(p) in on_file]
    new = [p for p in pairs if frozenset(p) not in on_file]
    # two different public profile pages are two different people
    cur.execute("""SELECT lead_id::text, value FROM lead_identities
                    WHERE kind = 'linkedin_url' AND value !~* '/in/ACoA' AND lead_id::text = ANY(%s)""",
                ([x for p in new for x in p],))
    pages = collections.defaultdict(set)
    for lead, value in cur.fetchall():
        pages[lead].add(value)
    ident = member_identity(cur, sorted({x for p in new for x in p}))
    rows = []
    for x, y in new:
        if pages[x] and pages[y] and not pages[x] & pages[y]:
            n["pairs dropped (two different LinkedIn pages)"] += 1
            continue
        if x not in ident or y not in ident:
            n["pairs dropped (a person with no identity)"] += 1
            continue
        root, key, people = pairs[(x, y)]
        root_name, root_kind = roots[root]
        rows.append({"ak": ident[x][0], "av": ident[x][1], "bk": ident[y][0], "bv": ident[y][1],
                     "basis": f"same_name_same_{root_kind}",
                     "evidence": {"name": key, "names_shown": [shown[x], shown[y]], "context": root_name,
                                  "context_id": root, "context_kind": root_kind,
                                  "seen_in": [sorted(people[x]), sorted(people[y])],
                                  "people_with_this_name": len(people)}})
    for part in chunks(rows):
        # the table wants the pair ordered; the database's own ordering decides which side is a
        cur.execute("""
            INSERT INTO lead_possible_matches (a_kind, a_value, b_kind, b_value, basis, evidence, source)
            SELECT CASE WHEN f THEN r->>'ak' ELSE r->>'bk' END, CASE WHEN f THEN r->>'av' ELSE r->>'bv' END,
                   CASE WHEN f THEN r->>'bk' ELSE r->>'ak' END, CASE WHEN f THEN r->>'bv' ELSE r->>'av' END,
                   r->>'basis', r->'evidence', %s
              FROM jsonb_array_elements(%s::jsonb) r,
                   LATERAL (SELECT (r->>'ak', r->>'av') < (r->>'bk', r->>'bv') AS f) o
            ON CONFLICT (a_kind, a_value, b_kind, b_value) DO UPDATE SET last_seen_at = now()""",
                    (f"{ME} match", json.dumps(part, ensure_ascii=False)))
        n["pairs filed"] += cur.rowcount
        conn.commit()
    for part in chunks(seen, 2000):
        cur.execute("UPDATE lead_possible_matches SET last_seen_at = now() WHERE id::text = ANY(%s)", (part,))
        n["pairs already on file, seen again"] += cur.rowcount
        conn.commit()
    cur.execute("SELECT state, count(*) FROM v_lead_possible_matches GROUP BY 1 ORDER BY 1")
    print("match:", dict(n), "| on file now:", dict(cur.fetchall()))
    return 0


def stage_daily(conn, a) -> int:
    for stage in (stage_chats, stage_join_requests, stage_match):
        stage(conn, a)
    return 0


def stage_report(conn, a) -> int:
    cur = conn.cursor()
    cur.execute("""SELECT c.kind, c.provider, count(DISTINCT c.id), count(m.id), count(DISTINCT v.lead_id)
                     FROM collectives c LEFT JOIN collective_memberships m ON m.collective_id = c.id
                     LEFT JOIN v_collective_members v ON v.membership_id = m.id
                    GROUP BY 1, 2 ORDER BY 1, 2""")
    print("kind, provider, collectives, memberships, people:")
    for r in cur.fetchall():
        print("  ", r)
    cur.execute("""SELECT application, attended, member_state, count(*) FROM collective_memberships
                    GROUP BY 1, 2, 3 ORDER BY 4 DESC""")
    print("application, attended, member_state, memberships:")
    for r in cur.fetchall():
        print("  ", r)
    cur.execute("SELECT count(*) FROM v_collective_members WHERE lead_id IS NULL")
    print("memberships with no person:", cur.fetchone()[0])
    cur.execute("SELECT state, count(*) FROM v_lead_possible_matches GROUP BY 1 ORDER BY 1")
    print("maybe the same person, by state:", dict(cur.fetchall()))
    cur.execute("SELECT to_regclass('v_lead_in_touch_reasons') IS NOT NULL")
    if cur.fetchone()[0]:
        cur.execute("SELECT basis, count(DISTINCT lead_id) FROM v_lead_in_touch_reasons GROUP BY 1 ORDER BY 1")
        by = dict(cur.fetchall())
        cur.execute("SELECT count(*) FROM v_leads_in_touch")
        print("personally in touch:", cur.fetchone()[0], "people;", by)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["events", "chats", "join-requests", "tag", "retire-tag", "propose", "decide",
                                      "match", "daily", "report"])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--client")
    g.add_argument("--all", action="store_true", help="daily: every instance")
    ap.add_argument("--tag")
    ap.add_argument("--name")
    ap.add_argument("--kind", default="community", choices=["group", "community", "event"])
    ap.add_argument("--attended", action="store_true")
    ap.add_argument("--message")
    ap.add_argument("--replaced-by")
    ap.add_argument("--id", help="decide: the collective")
    ap.add_argument("--parent", help="decide: id of the community it belongs to")
    ap.add_argument("--new-parent", help="decide: name of a community to create and put it in")
    ap.add_argument("--no-parent", action="store_true", help="decide: it belongs to no community")
    ap.add_argument("--company-id", help="decide: the company behind it")
    ap.add_argument("--no-company", action="store_true")
    ap.add_argument("--set-kind", choices=["group", "community", "event"])
    ap.add_argument("--by", help="decide: who decided")
    ap.add_argument("--monitored-only", action="store_true",
                    help="propose: keep only communities that hold a group the operator monitors")
    a = ap.parse_args()
    if a.stage == "tag" and not (a.tag and a.name):
        sys.exit("tag needs --tag and --name")
    if a.stage == "retire-tag" and not (a.tag and a.message):
        sys.exit("retire-tag needs --tag and --message")
    if a.stage == "decide" and not (a.id and a.by):
        sys.exit("decide needs --id and --by")
    if a.all and a.stage != "daily":
        sys.exit("--all goes with daily only")
    if a.all:
        failed = 0
        for client in discover_instances():
            print(f"== {client}", flush=True)
            conn = connect(client)
            try:
                stage_daily(conn, a)
            except Exception as e:  # noqa: BLE001  one instance's failure must not stop the others
                conn.rollback()
                failed += 1
                print(f"FAILED {client}: {type(e).__name__}: {e}", flush=True)
            finally:
                conn.close()
        return 1 if failed else 0
    conn = connect(a.client)
    try:
        return {"match": stage_match, "daily": stage_daily, "propose": stage_propose, "decide": stage_decide, "events": stage_events, "chats": stage_chats, "join-requests": stage_join_requests,
                "tag": stage_tag, "retire-tag": stage_retire_tag, "report": stage_report}[a.stage](conn, a)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
