#!/usr/bin/env python3
"""marketbase-file-verified-urns — record a member URN that a stored vendor
response already proved, on a lead that has no URN of its own, as a
lead_identity_conflicts finding against the lead that owns the URN. The pair
then appears in lead_merge_queue and merges like any other (tools/merge_leads.py,
owner-approved).

Why: merge_lead() (080) merges only a pair that shares a linkedin_urn identity.
A rep row added by its vanity URL and an engager row keyed on its URN-form URL
are often one person, proven by a lookup already paid for and cached in
enrichment_calls, but the URN was recorded on one row only.

Evidence, one stored response per finding, cited in the row's source:
  * blitz /v2/enrichment/person looked up by the URN-form URL answered with
    this lead's vanity URL;
  * fresh-linkedin-profile-data /enrich-lead looked up by this lead's vanity
    URL answered with the URN.
Only leads with no linkedin_urn identity and no member_urn; only URNs owned
by an unmerged lead; never a lead tied to two URNs. A pair whose names share
no name is HELD and listed, never filed (vendor indexes misattribute), unless
the owner reviewed it by hand and names it with --approve-held <lead id>.

Writes conflict rows only: no identity (the URN stays its owner's until the
merge moves it), no tags (the finding is the row; a tag pair would mark a lead
"never push" until the merge).

DEFAULT IS A DRY RUN. --execute --expect N commits only if N rows are filed.

Usage:
  python3 file_verified_urns.py --client X
  python3 file_verified_urns.py --client X --execute --expect N
  python3 file_verified_urns.py --client X --approve-held <lead id> [...] [--execute --expect N]
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

CANDIDATES = r"""
with norm as (
  select l.id, lower(rtrim(regexp_replace(l.linkedin_url, '^https?://([a-z]+\.)?linkedin\.com', '', 'i'), '/')) as path
    from leads l
   where l.merged_into is null and l.member_urn is null and l.linkedin_url is not null
     and l.linkedin_url !~* '/in/AC[A-Za-z0-9_-]{15,}'
     and not exists (select 1 from lead_identities i where i.lead_id = l.id and i.kind = 'linkedin_urn')
),
blitz as (   -- looked up by the URN, answered with this vanity URL
  select n.id as lead_id, substring(e.params->>'person_linkedin_url' from '/in/(AC[A-Za-z0-9_-]{15,})') as urn,
         e.id as call_id, 'blitz /v2/enrichment/person' as via
    from enrichment_calls e
    join norm n on n.path = lower(rtrim(regexp_replace(e.response->'person'->>'linkedin_url',
                                         '^https?://([a-z]+\.)?linkedin\.com', '', 'i'), '/'))
   where e.api = 'blitz' and e.endpoint = '/v2/enrichment/person' and e.success
     and e.params->>'person_linkedin_url' ~ '/in/ACoAA'
),
fresh as (   -- looked up by this vanity URL, answered with the URN
  select n.id as lead_id, e.response->'data'->>'urn' as urn, e.id as call_id,
         'fresh-linkedin-profile-data /enrich-lead' as via
    from enrichment_calls e
    join norm n on n.path = lower(rtrim(regexp_replace(e.params->>'linkedin_url',
                                         '^https?://([a-z]+\.)?linkedin\.com', '', 'i'), '/'))
   where e.api = 'fresh-linkedin-profile-data' and e.endpoint = '/enrich-lead' and e.success
     and e.response->'data'->>'urn' like 'ACoAA%'
),
ev as (select * from blitz union all select * from fresh),
own as (
  select ev.*, i.lead_id as owner_lead_id
    from ev join lead_identities i on i.kind = 'linkedin_urn' and i.value = ev.urn
    join leads o on o.id = i.lead_id and o.merged_into is null
   where i.lead_id <> ev.lead_id
)
select distinct on (lead_id, urn) lead_id, urn, owner_lead_id, call_id, via,
       (select count(distinct o2.urn) from own o2 where o2.lead_id = own.lead_id) as urns_for_lead
  from own
 order by lead_id, urn, via, call_id
"""


def _names(n: str | None) -> list[str]:
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode().lower()
    return [t for t in re.split(r"[^a-z]+", n) if len(t) > 1 and t != "none"]


def names_agree(a: str | None, b: str | None) -> bool:
    x, y = _names(a), _names(b)
    return bool(x and y and set(x) & set(y))


def main() -> int:
    ap = argparse.ArgumentParser(description="File vendor-verified URNs as merge findings.")
    ap.add_argument("--client", required=True)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--expect", type=int, help="with --execute: the approved number of rows to file")
    ap.add_argument("--approve-held", action="append", default=[], metavar="LEAD_ID",
                    help="a held lead the owner reviewed by hand: file it although the names differ")
    args = ap.parse_args()
    if args.execute and args.expect is None:
        sys.exit("--execute needs --expect N from the dry run")

    conn = lib.connect(args.client)
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '600s'")
    cur.execute("SET lock_timeout = '10s'")
    cur.execute(f"""SELECT e.lead_id::text, e.urn, e.owner_lead_id::text, e.call_id::text, e.via,
                           e.urns_for_lead, l.name, o.name
                      FROM ({CANDIDATES}) e
                      JOIN leads l ON l.id = e.lead_id JOIN leads o ON o.id = e.owner_lead_id""")
    rows = cur.fetchall()
    file, held = [], []
    for lead, urn, owner, call, via, n_urns, lname, oname in rows:
        if n_urns != 1:
            held.append((lead, owner, lname, oname, "tied to more than one URN"))
        elif not names_agree(lname, oname) and lead not in args.approve_held:
            held.append((lead, owner, lname, oname, "names share nothing"))
        elif not names_agree(lname, oname):
            file.append((lead, urn, owner, f"file_verified_urns: {via} enrichment_calls {call}; "
                                           f"names differ, reviewed and approved by the owner"))
        else:
            file.append((lead, urn, owner, f"file_verified_urns: {via} enrichment_calls {call}"))

    unknown = set(args.approve_held) - {r[0] for r in rows}
    if unknown:
        conn.rollback()
        sys.exit(f"--approve-held names lead(s) with no evidence-backed finding: {', '.join(sorted(unknown))}")
    print(f"{args.client}: {len(rows)} evidence-backed finding(s); {len(file)} to file, {len(held)} held")
    print("  by evidence: " + ", ".join(f"{k} {v}" for k, v in
                                       collections.Counter(s.split(": ")[1].rsplit(" enrichment_calls", 1)[0]
                                                           for *_, s in file).items()))
    for lead, owner, lname, oname, why in held:
        print(f"  HELD ({why}): {lead} {lname!r}  vs owner {owner} {oname!r}")

    if not args.execute:
        conn.rollback()
        print(f"Dry run: nothing written. To file, after approval: --execute --expect {len(file)}")
        return 0
    if args.expect != len(file):
        conn.rollback()
        sys.exit(f"Refusing: {len(file)} finding(s) now, not the approved {args.expect}.")
    n = 0
    for lead, urn, owner, source in file:
        cur.execute("""INSERT INTO lead_identity_conflicts (lead_id, kind, value, owner_lead_id, source)
                       VALUES (%s, 'linkedin_urn', %s, %s, %s)
                       ON CONFLICT (lead_id, kind, value) DO NOTHING""", (lead, urn, owner, source))
        n += cur.rowcount
    if n != args.expect:
        conn.rollback()
        sys.exit(f"Refusing: filed {n}, expected {args.expect}; rolled back.")
    cur.execute("SELECT count(*) FROM lead_merge_queue WHERE conflict_id IN "
                "(SELECT id FROM lead_identity_conflicts WHERE source LIKE 'file_verified_urns:%%')")
    queued = cur.fetchone()[0]
    conn.commit()
    print(f"FILED {n} finding(s); {queued} now in lead_merge_queue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
