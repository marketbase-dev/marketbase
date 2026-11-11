#!/usr/bin/env python3
"""marketbase-file-member-ids: file the LinkedIn member ID (ACoAA...) that a paid profile
lookup OBSERVES, for leads that have none, and settle the URL conflicts that can never get
one. Deletes nothing.

Why: merge_lead() merges two rows only when both carry the same verified member ID. Two kinds
of lead lack one:
  * the rows of an open `linkedin_url` conflict (the URL is already another lead's identity);
  * leads whose only LinkedIn ID is the search form (ACwAA..., what search results return),
    which is not a member ID and cannot be compared with one.
A lookup of the lead's own profile link answers with its member ID. Filing it through
lead_identity_claim(), the one identity write path, either lands it (`inserted`) or records
the same person twice (`duplicate:<lead>`), which puts the pair in lead_merge_queue for
merge_leads.py.

A dead handle: the lookup of a lead's URL answers "not found" (the person changed their vanity
URL and LinkedIn does not redirect the old one), and that URL is the lead's side of an open
`linkedin_url` conflict, so the other lead already holds it. No member ID can ever be observed
for that row, so the tool records the decision instead of leaving it to be asked again: the
dead row gets flag:duplicate_lead + expansion:do_not_push through the claim's duplicate path,
and the conflict row's `source` gains a "DECIDED ... leave unmerged" note. --url-conflicts
skips conflicts already marked DECIDED.

Phases:
  (default)  Read-only plan from lookups already in enrichment_calls, of any age and any
             request form (older rows carry include_skills, and a cache keyed on exact params
             would buy them again). Reports how many leads still need a paid lookup. The
             whole plan runs in one transaction that is rolled back. Prints a plan digest.
  --fetch    First makes those paid lookups, one at a time, and stores every answer, a
             "not found" included, since it is the evidence for a dead handle. A 429
             per-minute refusal waits 61 s and retries. Then plans as above.
  --execute  Needs --plan <digest> --expect <writes> --by <who approved>. Re-plans from the
             cache only (it never fetches), refuses if the digest or the write count
             differs, and commits one transaction whose invariants are checked inside it.

A lookup is filed only when the lead's first name appears in the profile's full name.
--accept-name LEAD_ID:FIRST accepts a nickname or spelling variant checked by hand.

Usage:
  python3 file_member_ids.py --client <name> --url-conflicts
  python3 file_member_ids.py --client <name> --search-id-only --fetch
  python3 file_member_ids.py --client <name> --lead <uuid> [--lead <uuid> ...]
  python3 file_member_ids.py --client <name> --url-conflicts \\
      --execute --plan <digest> --expect <n> --by <name>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROVIDER = "fresh-linkedin-profile-data"
ENDPOINT = "/enrich-lead"
HOST = "fresh-linkedin-profile-data.p.rapidapi.com"
TOOL = "file_member_ids"
MEMBER = re.compile(r"ACoAA[A-Za-z0-9_\-]{10,}")
SEARCH_FORM = "ACwAA"


# ── evidence ─────────────────────────────────────────────────────────────────

def evidence(cur, url: str):
    """The cached answer for this URL: (call_id, fetched_at, success, response) or None.
    Any request form and any age; a success wins over a failure, then the newest."""
    cur.execute("""SELECT id::text, fetched_at, success, response FROM enrichment_calls
                    WHERE api = %s AND endpoint = %s AND params->>'linkedin_url' = %s
                    ORDER BY success DESC, fetched_at DESC LIMIT 1""", (PROVIDER, ENDPOINT, url))
    return cur.fetchone()


def observed_member_id(resp) -> str | None:
    data = (resp or {}).get("data") if isinstance(resp, dict) else None
    m = MEMBER.search((data or {}).get("urn") or "") if isinstance(data, dict) else None
    return m.group(0) if m else None


def answered_not_found(resp) -> bool:
    """The provider's definitive "no profile at this URL", as opposed to a timeout or an error."""
    return isinstance(resp, dict) and "not found" in str(resp.get("message") or "").lower()


def _fold(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", s)


def first_name(name: str | None) -> str:
    words = [w for w in _fold(name).split() if len(w) > 1]
    return words[0] if words else ""


def name_matches(lead_name, full_name, accepted: str | None) -> bool:
    tokens = _fold(full_name).split()
    if accepted:
        return tokens[:1] == [accepted.lower()]
    fn = first_name(lead_name)
    return bool(fn) and fn in tokens


# ── selection ────────────────────────────────────────────────────────────────

# A lead already has its member ID when it holds one, or when its claim on one was recorded
# as the same person as another lead (an open linkedin_urn conflict): that pair waits in
# lead_merge_queue, and looking it up again would only repeat the finding.
HAS_MEMBER_ID = """(EXISTS (SELECT 1 FROM lead_identities i WHERE i.lead_id = l.id
                           AND i.kind = 'linkedin_urn' AND i.value LIKE 'ACoAA%%')
                   OR EXISTS (SELECT 1 FROM lead_identity_conflicts k WHERE k.lead_id = l.id
                           AND k.kind = 'linkedin_urn' AND k.resolved_by_merge IS NULL
                           AND k.value LIKE 'ACoAA%%'))"""


def select_targets(cur, mode: str, lead_ids=()) -> tuple[dict, list]:
    """({lead_id: {name, url}}, [conflict dicts]) for the leads that lack a member ID."""
    conflicts = []
    if mode == "url-conflicts":
        cur.execute("""SELECT c.id::text, c.lead_id::text, c.owner_lead_id::text, c.value
                         FROM lead_identity_conflicts c
                         JOIN leads l ON l.id = c.lead_id
                         JOIN leads o ON o.id = c.owner_lead_id
                        WHERE c.kind = 'linkedin_url' AND c.resolved_by_merge IS NULL
                          AND l.merged_into IS NULL AND o.merged_into IS NULL
                          AND coalesce(c.source, '') NOT LIKE '%%DECIDED%%'
                        ORDER BY c.id""")
        conflicts = [dict(zip(("id", "lead", "other", "value"), r)) for r in cur.fetchall()]
        ids = sorted({c["lead"] for c in conflicts} | {c["other"] for c in conflicts})
    elif mode == "search-id-only":
        cur.execute(f"""SELECT l.id::text FROM leads l
                         WHERE l.merged_into IS NULL AND l.linkedin_url IS NOT NULL
                           AND EXISTS (SELECT 1 FROM lead_identities i WHERE i.lead_id = l.id
                                         AND i.value LIKE '{SEARCH_FORM}%%')
                           AND NOT {HAS_MEMBER_ID}
                         ORDER BY l.id""")
        ids = [r[0] for r in cur.fetchall()]
    else:
        ids = [str(uuid.UUID(i)) for i in lead_ids]
    targets = {}
    if ids:
        cur.execute(f"""SELECT l.id::text, l.name, l.linkedin_url, l.merged_into IS NOT NULL, {HAS_MEMBER_ID}
                          FROM leads l WHERE l.id = ANY(%s::uuid[])""", (ids,))
        for lid, name, url, merged, has in cur.fetchall():
            if not merged and not has and url:
                targets[lid] = {"name": name, "url": url}
    return targets, conflicts


# ── plan (and, on --execute, the writes) ─────────────────────────────────────

def plan(cur, targets: dict, conflicts: list, by: str, accept: dict) -> dict:
    """Run every claim inside the caller's transaction. Returns the plan: lines (for the digest),
    writes, the per-lead report, what still needs a lookup, and failed invariants. The caller
    commits or rolls back."""
    lines, report, need_lookup, fails = [], [], [], []
    observed = {}
    ev = {lid: evidence(cur, t["url"]) for lid, t in targets.items()}
    leads = sorted(targets)

    def snapshot():
        if not leads:
            return (0, 0, 0, None)
        cur.execute("SELECT count(*) FROM lead_identities WHERE lead_id = ANY(%s::uuid[])", (leads,))
        ids = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM lead_identity_conflicts WHERE lead_id = ANY(%s::uuid[])", (leads,))
        conf = cur.fetchone()[0]
        cur.execute("""SELECT count(*) FROM lead_tags WHERE lead_id = ANY(%s::uuid[])
                         AND tag IN ('flag:duplicate_lead', 'expansion:do_not_push')""", (leads,))
        tags = cur.fetchone()[0]
        cur.execute("SELECT md5(string_agg(l::text, ',' ORDER BY l.id)) FROM leads l WHERE l.id = ANY(%s::uuid[])",
                    (leads,))
        return (ids, conf, tags, cur.fetchone()[0])

    before = snapshot()
    expect_ids = expect_conf = expect_tags = 0

    def tags_missing(lid):
        cur.execute("""SELECT 2 - count(*) FROM lead_tags WHERE lead_id = %s
                         AND tag IN ('flag:duplicate_lead', 'expansion:do_not_push')""", (lid,))
        return cur.fetchone()[0]

    # Conflict holders first, so a pair whose two rows resolve to one member ID keeps the
    # row that already held the URL.
    holders = {c["other"] for c in conflicts}
    for lid in sorted(leads, key=lambda x: (x not in holders, x)):
        t, e = targets[lid], ev[lid]
        label = f"{t['name']} ({lid[:8]})"
        if e is None:
            need_lookup.append(lid)
            report.append(f"  lookup  {label}: no cached answer for {t['url']}")
            continue
        call, at, _ok, resp = e
        mid = observed_member_id(resp)
        if mid:
            full = ((resp or {}).get("data") or {}).get("full_name")
            if not name_matches(t["name"], full, accept.get(lid)):
                report.append(f"  skip    {label}: the profile's name is {full!r}; check by hand, then --accept-name")
                continue
            cur.execute("SELECT count(*) FROM lead_identity_conflicts WHERE lead_id = %s AND kind = 'linkedin_urn' "
                        "AND value = %s", (lid, mid))
            had_conflict = cur.fetchone()[0] > 0
            missing = tags_missing(lid)
            src = f"observed: {PROVIDER} {ENDPOINT} enrichment_calls {call} ({TOOL})"
            cur.execute("SELECT lead_identity_claim(%s, 'linkedin_urn', %s, %s, true, %s)", (lid, mid, src, at))
            res = cur.fetchone()[0]
            observed[lid] = mid
            if res == "inserted":
                expect_ids += 1
            elif res.startswith("duplicate:"):
                expect_conf += 0 if had_conflict else 1
                expect_tags += missing
            elif res != "ours":
                fails.append(f"{label}: unexpected claim outcome {res}")
            lines.append(f"member_id|{lid}|{mid}|{call}")
            report.append(f"  file    {label} -> {mid}  [{res if not res.startswith('duplicate:') else 'same person as ' + res[10:18]}]")
            continue
        if answered_not_found(resp):
            dead = [c for c in conflicts if c["lead"] == lid and c["value"] == t["url"]]
            if not dead:
                report.append(f"  gone    {label}: no profile at {t['url']} (enrichment_calls {call[:8]}); no rule applies, "
                              "a person decides")
                continue
            for c in dead:
                missing = tags_missing(lid)
                src = f"{TOOL}: dead handle, enrichment_calls {call} found no profile; decided by {by}"
                cur.execute("SELECT lead_identity_claim(%s, 'linkedin_url', %s, %s, false)", (lid, c["value"], src))
                res = cur.fetchone()[0]
                if res != f"duplicate:{c['other']}":
                    fails.append(f"{label}: dead-handle claim returned {res}, expected duplicate:{c['other']}")
                expect_tags += missing
                note = (f" | DECIDED {time.strftime('%Y-%m-%d')} by {by}: leave unmerged. Dead handle: lookup {call} "
                        f"found no profile at this URL, so no member ID can be observed; the row carries "
                        f"flag:duplicate_lead + expansion:do_not_push ({TOOL})")
                cur.execute("UPDATE lead_identity_conflicts SET source = coalesce(source, '') || %s "
                            "WHERE id = %s AND resolved_by_merge IS NULL", (note, c["id"]))
                if cur.rowcount != 1:
                    fails.append(f"{label}: conflict {c['id'][:8]} note rowcount {cur.rowcount}")
                lines.append(f"dead_handle|{lid}|{c['value']}|{call}|{c['id']}")
                report.append(f"  decided {label}: dead handle {c['value']} (same person as {c['other'][:8]}); "
                              "tagged, conflict marked DECIDED")
            continue
        status = (resp or {}).get("_http_status") if isinstance(resp, dict) else None
        report.append(f"  noreply {label}: the cached lookup gave no member ID (status {status}); --fetch retries it")
        need_lookup.append(lid)

    # Two rows of one conflict that resolved to DIFFERENT member IDs are two people sharing a
    # URL identity: nothing to merge, and a person should look.
    for c in conflicts:
        a, b = observed.get(c["lead"]), observed.get(c["other"])
        if a and b and a != b:
            report.append(f"  differ  conflict {c['id'][:8]}: {c['lead'][:8]} is {a}, {c['other'][:8]} is {b}; "
                          "not the same person, a person decides")

    after = snapshot()
    if leads:
        checks = {
            f"identities +{expect_ids}": after[0] == before[0] + expect_ids,
            f"conflicts +{expect_conf}": after[1] == before[1] + expect_conf,
            f"duplicate tags +{expect_tags}": after[2] == before[2] + expect_tags,
            "lead rows unchanged": after[3] == before[3],
        }
        fails += [k for k, ok in checks.items() if not ok]
    digest = hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()[:16]
    return {"lines": lines, "writes": len(lines), "report": report, "need_lookup": need_lookup,
            "fails": fails, "digest": digest}


# ── fetch ────────────────────────────────────────────────────────────────────

def fetch_one(key: str, url: str):
    q = urllib.parse.urlencode({"linkedin_url": url, "include_skills": "false"})
    req = urllib.request.Request(f"https://{HOST}{ENDPOINT}?{q}",
                                 headers={"x-rapidapi-key": key, "x-rapidapi-host": HOST})
    try:
        r = urllib.request.urlopen(req, timeout=300)
        return r.status, r.read().decode(errors="replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), dict(e.headers)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}", {}


def fetch_missing(conn, key: str, targets: dict, lead_ids: list) -> None:
    """Paid lookups for these leads, one at a time, every answer stored (api_cache.put commits)."""
    import api_cache
    print(f"{len(lead_ids)} paid lookup(s) to make", flush=True)
    for i, lid in enumerate(lead_ids, 1):
        url = targets[lid]["url"]
        for _attempt in range(4):
            status, body, headers = fetch_one(key, url)
            if status != 429:
                break
            time.sleep(61)
        try:
            resp = json.loads(body) if status is not None else None
        except json.JSONDecodeError:
            resp = {"_raw": body[:2000]}
        if isinstance(resp, dict):
            resp["_http_status"] = status
        ok = status == 200 and observed_member_id(resp) is not None
        api_cache.put(conn, PROVIDER, ENDPOINT, {"linkedin_url": url, "include_skills": "false"}, ok, resp,
                      error=None if status is not None else body[:500])
        print(f"  [{i}/{len(lead_ids)}] {status} {'member ID' if ok else 'no member ID'}  "
              f"credits {headers.get('X-RateLimit-credits-Remaining') or headers.get('x-ratelimit-credits-remaining')}"
              f"  {lid[:8]}", flush=True)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="File observed LinkedIn member IDs; settle dead-handle URL conflicts. "
                                             "Deletes nothing.")
    ap.add_argument("--client", required=True)
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--url-conflicts", action="store_true", help="both rows of every open, undecided linkedin_url conflict")
    sel.add_argument("--search-id-only", action="store_true", help="leads whose only LinkedIn ID is the search form")
    sel.add_argument("--lead", action="append", help="a lead id (repeatable)")
    ap.add_argument("--fetch", action="store_true", help="make the missing paid lookups first")
    ap.add_argument("--accept-name", action="append", default=[], help="LEAD_ID:FIRST, a name variant checked by hand")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--plan", help="digest of the approved dry run (required with --execute)")
    ap.add_argument("--expect", type=int, help="writes in the approved dry run (required with --execute)")
    ap.add_argument("--by", help="who approved (required with --execute); recorded in the DECIDED note")
    args = ap.parse_args()
    if args.execute and (not args.plan or args.expect is None or not args.by):
        ap.error("--execute needs --plan, --expect and --by")
    if args.execute and args.fetch:
        ap.error("--execute never fetches; run --fetch in a dry run first")
    accept = {}
    for a in args.accept_name:
        lid, _, first = a.partition(":")
        accept[str(uuid.UUID(lid))] = first.strip().lower()
    mode = "url-conflicts" if args.url_conflicts else "search-id-only" if args.search_id_only else "lead"

    import lib
    conn = lib.connect(args.client)
    cur = conn.cursor()
    targets, conflicts = select_targets(cur, mode, args.lead or ())
    print(f"{args.client}: {len(targets)} lead(s) without a member ID"
          + (f", in {len(conflicts)} open undecided conflict(s)" if mode == "url-conflicts" else ""), flush=True)
    if args.fetch:
        missing = [lid for lid, t in targets.items()
                   if not ((e := evidence(cur, t["url"])) and (observed_member_id(e[3]) or answered_not_found(e[3])))]
        conn.rollback()
        env = lib.load_client_env(args.client)
        key = os.environ.get("RAPIDAPI_KEY") or env.get("RAPIDAPI_KEY")
        if not key:
            sys.exit("RAPIDAPI_KEY missing")
        fetch_missing(conn, key, targets, missing)

    p = plan(cur, targets, conflicts, args.by or "<approver>", accept)
    for line in p["report"]:
        print(line)
    print(f"\n{p['writes']} write(s); {len(p['need_lookup'])} lead(s) still need a paid lookup"
          + (" (run with --fetch)" if p["need_lookup"] else ""))
    print(f"plan digest: {p['digest']}")
    if p["fails"]:
        conn.rollback()
        print("REFUSED, rolled back: " + "; ".join(p["fails"]))
        return 1
    if not args.execute:
        conn.rollback()
        print("dry run: nothing written" + (f". To write, after approval: --execute --plan {p['digest']} "
                                           f"--expect {p['writes']} --by <name>" if p["writes"] else ""))
        return 0
    if p["digest"] != args.plan or p["writes"] != args.expect:
        conn.rollback()
        print(f"REFUSED: plan {p['digest']} / {p['writes']} writes is not the approved {args.plan} / {args.expect}")
        return 1
    conn.commit()
    print(f"COMMITTED {p['writes']} write(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
