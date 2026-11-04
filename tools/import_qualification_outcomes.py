#!/usr/bin/env python3
"""Import a Buyer-Monitor webhook OUTCOME log into a client's MarketBase.

This is the feedback half of the loop. The webhook log says who we pushed; this
says what the client's own system decided about each of them — whether the person
qualified, whether they are eligible for outreach, and why not when they are not.

The file is MULTI-CLIENT (one row per verdict across every Buyer-Monitor client),
so rows are filtered on the embedded `Client` field. Clients also emit different
schemas for the same idea, and all of them are normalised here:

    Acme :  Person Qualification Status = Pass | Fail
                 Person Outreach Eligibility = Passed | Suppressed - <reason>
                 Company Outreach Eligibility = Eligible | Not Eligible
    <Client>  :  Person Qualification Status = true | false
                 Company Outreach Eligibility = true | false
                 (no Person Outreach Eligibility field at all)
    Northwind :  Person Qualification Status = Accepted | DQ
                 (its own words: pass them with --qualified-words / --failed-words)

Status vocabulary. Clients spell "accepted" and "rejected" in their own words, so
the caller should pass the client's vocabulary (--qualified-words accepted
--failed-words dq) from wherever it is configured. With a vocabulary, a status word
in neither list is reported as UNRECOGNISED and not filed: it is not a rejection,
and treating an unknown word as one is how a client's acceptances once read as
zero. Without a vocabulary, the legacy TRUE set below applies.

A payload with no status at all carries no verdict and is counted `no_verdict`,
never filed as a failed qualification.

Writes to `lead_qualifications` under a per-client qualifier name, so the client's
verdict is queryable next to our own qualifiers without pretending to be one, plus
tags for cheap selection:

    outcome:qualified_eligible   qualified AND cleared for outreach  <- the useful set
    outcome:failed_qual          the person did not qualify
    outcome:suppressed           qualified, but held back (pipeline, customer, lost)
    outcome:<suppression reason>  e.g. outcome:suppressed_pipeline

The suppression reasons are worth keeping: "Suppressed - Pipeline" / "Lost
Opportunity" / "Customer" is exactly the deal-state signal that keeps outreach off
accounts that are already in play.

Usage:
  python3 import_qualification_outcomes.py --client Acme \
      --csv ".../webhook-outcome-logs-2026-08-12.csv" [--client-key Acme] [--dry-run]
  python3 import_qualification_outcomes.py --client Northwind --csv - \
      --qualified-words accepted --failed-words dq      # rows on stdin, never on disk
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import connect, member_urn_token, normalize_linkedin_url, lead_ids_for_linkedin_urls  # noqa: E402

from psycopg.types.json import Jsonb  # noqa: E402

csv.field_size_limit(10 ** 7)

QUALIFIER_VERSION = "1.0"
# Fallback vocabulary, used only when the caller passes none.
TRUE = {"pass", "true", "yes", "eligible", "passed"}


def truthy(v) -> bool:
    return str(v).strip().lower() in TRUE


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def words(arg: str) -> set[str]:
    return {w.strip().lower() for w in (arg or "").split(",") if w.strip()}


def status_reading(d: dict, qualified_words: set[str] | None,
                   failed_words: set[str] | None) -> str:
    """'qualified' | 'failed' | 'blank' | 'unrecognised' for the status field."""
    s = str(d.get("Person Qualification Status") or "").strip().lower()
    if not s:
        return "blank"
    if qualified_words is None:
        return "qualified" if s in TRUE else "failed"
    if s in qualified_words:
        return "qualified"
    if failed_words is not None and s in failed_words:
        return "failed"
    return "unrecognised"


def verdict(d: dict, qualified_words: set[str] | None = None) -> tuple[bool, bool, str]:
    """(person_qualified, outreach_ok, disqualified_reason) across the schemas."""
    q = truthy(d.get("Person Qualification Status")) if qualified_words is None else (
        str(d.get("Person Qualification Status") or "").strip().lower() in qualified_words)
    pe = d.get("Person Outreach Eligibility")
    ce = d.get("Company Outreach Eligibility")
    # Eligibility may carry the client's own words too (some echo the status into it).
    ok_words = TRUE | (qualified_words or set())
    elig = lambda v: str(v).strip().lower() in ok_words  # noqa: E731
    # Person-level eligibility wins when present; otherwise fall back to company
    # level, which is all the true/false schema emits.
    ok = elig(pe) if pe is not None else elig(ce)
    if q and not ok:
        reason = str(pe or ce or "").strip() or "suppressed"
    elif not q:
        reason = str(pe or "").strip() if str(pe or "").lower().startswith("suppressed") else "failed_qualification"
    else:
        reason = ""
    return q, ok, reason


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--csv", required=True,
                    help="path to the outcome CSV, or '-' to read it from stdin, so "
                         "verdicts fetched from an API never have to touch disk")
    ap.add_argument("--client-key", default="", help="value of the embedded Client field "
                                                     "(defaults to --client)")
    ap.add_argument("--qualifier", default="", help="defaults to <client-key>_clay_outcome")
    ap.add_argument("--qualified-words", default=None,
                    help="comma list of status words meaning ACCEPTED for this client. "
                         "With it, a status in neither list is reported as unrecognised, "
                         "never filed as a rejection.")
    ap.add_argument("--failed-words", default=None,
                    help="comma list of status words meaning REJECTED (see --qualified-words)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retag-only", action="store_true",
                    help="skip the qualification inserts, just recompute the outcome:* "
                         "tags from this file (last verdict per lead wins)")
    args = ap.parse_args()

    client_key = args.client_key or args.client
    qualifier = args.qualifier or f"{slug(client_key)}_clay_outcome"
    qw = words(args.qualified_words) if args.qualified_words is not None else None
    fw = words(args.failed_words) if args.failed_words is not None else None
    unrecognised = Counter()

    if args.csv == "-":
        src_name = "stdin"
        fh = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8-sig", newline="")
    else:
        src_name = Path(args.csv).name
        fh = Path(args.csv).open(encoding="utf-8-sig", newline="")

    rows, skipped_client, unparseable = [], 0, 0
    for r in csv.DictReader(fh):
        try:
            d = json.loads(r["qualification_results"])
        except Exception:
            unparseable += 1
            continue
        if (d.get("Client") or "") != client_key:
            skipped_client += 1
            continue
        # Exports vary: some carry id + createdAt columns, some are just the JSON
        # blob. Fall back to a content hash so idempotency survives either shape —
        # an identical verdict is skipped, a CHANGED verdict hashes differently and
        # is recorded as a new row (which is how a re-qualification shows up).
        d["_row_id"] = r.get("id") or hashlib.sha1(
            json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        d["_created_at"] = r.get("createdAt")
        rows.append(d)

    print(f"{src_name}: {len(rows)} rows for Client='{client_key}' "
          f"({skipped_client} other clients, {unparseable} unparseable)")
    if not rows:
        return 0

    buckets = Counter()
    stats = {"written": 0, "already": 0, "no_lead": 0, "tags": 0}
    missing: list[str] = []

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            urls = [normalize_linkedin_url(d.get("URL") or "") for d in rows]
            ids = lead_ids_for_linkedin_urls(cur, urls)
            # Fall back to the stable member URN for anything the URL string missed.
            # A verdict can arrive keyed on a member-URN URL while the lead is stored
            # under the other URN namespace or a vanity URL: one person, strings that
            # match on nothing. `member_urn` is the identity, so it recovers them.
            unmatched = {u for u in urls if u and u not in ids}
            if unmatched:
                recovered = 0
                for u in unmatched:
                    tok = member_urn_token(u)
                    if not tok:
                        continue
                    cur.execute("SELECT id FROM leads WHERE member_urn = %s LIMIT 1", (tok,))
                    row = cur.fetchone()
                    if row:
                        ids[u] = row[0]
                        recovered += 1
                if recovered:
                    print(f"  recovered {recovered} lead(s) by member_urn that the "
                          f"URL string missed")
            cur.execute("""SELECT full_result->>'_row_id' FROM lead_qualifications
                           WHERE qualifier_name = %s""", (qualifier,))
            done = {r[0] for r in cur.fetchall() if r[0]}
            print(f"leads matched: {len(ids)}/{len(rows)} | already imported: {len(done)}")

            pending_q, latest_tags = [], {}
            for d, url in zip(rows, urls):
                reading = status_reading(d, qw, fw)
                if reading == "blank":
                    # No status, no verdict: filing it as failed_qual would invent
                    # a rejection nobody made.
                    buckets["no_verdict"] += 1
                    continue
                if reading == "unrecognised":
                    buckets["unrecognised_status"] += 1
                    unrecognised[str(d.get("Person Qualification Status")).strip()] += 1
                    continue
                q, ok, reason = verdict(d, qw)
                bucket = ("qualified_eligible" if (q and ok)
                          else "failed_qual" if not q else "suppressed")
                buckets[bucket] += 1
                lead_id = ids.get(url)
                if not lead_id:
                    stats["no_lead"] += 1
                    missing.append(url)
                    continue
                if str(d.get("_row_id")) in done:
                    stats["already"] += 1

                tags = [f"outcome:{bucket}"]
                if bucket == "suppressed" or (not q and reason.lower().startswith("suppressed")):
                    tags.append(f"outcome:suppressed_{slug(reason.replace('Suppressed -', ''))}"
                                .rstrip("_"))
                if str(d.get("_row_id")) not in done:
                    pending_q.append((lead_id, qualifier, QUALIFIER_VERSION, q and ok,
                                      d.get("Person Qualification Segment") or None,
                                      d.get("Qualification Reasoning") or None,
                                      reason or None, Jsonb(d)))
                # LAST verdict in the file wins. A re-qualified lead appears twice
                # (the old Fail and the new Pass); tagging both would leave it
                # carrying outcome:failed_qual AND outcome:qualified_eligible.
                latest_tags[lead_id] = [(lead_id, t, d.get("_created_at") or "") for t in tags]

            print("\noutcome buckets (all rows for this client):")
            for b, n in buckets.most_common():
                print(f"    {n:>5}  {b}")
            if unrecognised:
                print("  status words the vocabulary does not know (NOT filed; add them "
                      "to --qualified-words / --failed-words):")
                for w, n in unrecognised.most_common():
                    print(f"    {n:>5}  {w!r}")

            pending_t = [t for tags_ in latest_tags.values() for t in tags_]
            if not args.dry_run and (pending_q or (args.retag_only and pending_t)):
                if pending_q and not args.retag_only:
                    cur.executemany("""INSERT INTO lead_qualifications
                                         (lead_id, qualifier_name, qualifier_version, qualified,
                                          persona, reason, disqualified_reason, full_result, qualified_at)
                                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())""", pending_q)
                    stats["written"] = len(pending_q)
                # A re-qualification must not leave the OLD verdict's tag behind:
                # a lead that flipped Fail -> Pass would otherwise carry both
                # outcome:failed_qual and outcome:qualified_eligible forever.
                touched = list(latest_tags)
                cur.execute("""DELETE FROM lead_tags
                               WHERE lead_id = ANY(%s) AND tag LIKE 'outcome:%%'""", (touched,))
                stats["stale_tags_cleared"] = cur.rowcount
                cur.executemany("""INSERT INTO lead_tags (lead_id, tag, notes, tagged_by)
                                   VALUES (%s,%s,%s,'import_qualification_outcomes')
                                   ON CONFLICT (lead_id, tag) DO NOTHING""", pending_t)
                stats["tags"] = len(pending_t)
        if not args.dry_run:
            conn.commit()

    print("\n=== summary ===")
    for k, v in stats.items():
        print(f"  {k:>12}: {v}")
    if missing:
        print(f"  (no lead row for {len(missing)} URLs — e.g. {missing[0]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
