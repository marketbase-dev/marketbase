#!/usr/bin/env python3
"""Exercise the voice tables and the poster resolver (schema/073, targets since 087) against a
database that already has every migration applied. Everything runs inside ONE
transaction that is always rolled back: no row survives, on any instance.

Why this exists: voices are keyed by entity id, and posts learn their author's
id from a SQL trigger. Neither is visible to the Python checks. This proves the
constraints refuse what they should, the resolver matches by identity and
never by guess, and v_voices works on an empty table and on seeded rows.

Usage:
  python3 scripts/check_voices.py                 # MARKETBASE_URL (CI)
  python3 scripts/check_voices.py --url postgresql://...
  python3 scripts/check_voices.py --instance <name>
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

CHECK, UNIQUE, FK = "23514", "23505", "23503"
IN = "https://www.linkedin.com/in/"
URN = "ACoAA" + "TEST" * 8 + "09"   # shaped like a member URN, low entropy


def _connect(args):
    if args.instance:
        import lib
        return lib.connect(args.instance)
    url = args.url or os.environ.get("MARKETBASE_URL")
    if not url:
        sys.exit("No database. Pass --url, --instance, or set MARKETBASE_URL.")
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

    def attempt(sql, params=()):
        cur.execute("SAVEPOINT v")
        try:
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
            cur.execute("RELEASE SAVEPOINT v")
            return None, row
        except Exception as e:  # noqa: BLE001 - the error IS the result
            cur.execute("ROLLBACK TO SAVEPOINT v")
            return _sqlstate(e), None

    def refused(name, sql, params=(), code=CHECK):
        state, _ = attempt(sql, params)
        check(name, state == code, f"got sqlstate {state}")

    def one(sql, params=()):
        cur.execute(sql, params)
        return cur.fetchone()

    # ── empty table ──
    cur.execute("SELECT count(*) FROM targets WHERE added_by='check_voices'")
    before = cur.fetchone()[0]
    cur.execute("SELECT * FROM v_voices WHERE added_by='check_voices'")
    check("v_voices works with no voices", cur.fetchall() == [] and before == 0)

    # ── fixtures ──
    page = one("INSERT INTO companies (linkedin_slug, name) VALUES ('zz-voice-page', 'ZZ Page') RETURNING id")[0]
    show = one("INSERT INTO companies (linkedin_slug, name, is_showcase, parent_linkedin_slug) "
               "VALUES ('zz-voice-show', 'ZZ Show', true, 'zz-voice-page') RETURNING id")[0]
    vanity = one("INSERT INTO leads (linkedin_url, name) VALUES (%s, 'ZZ Vanity') RETURNING id",
                 (IN + "zz-voice-vanity",))[0]
    urn_lead = one("INSERT INTO leads (linkedin_url, member_urn, name) VALUES (%s, %s, 'ZZ Urn') RETURNING id",
                   (IN + "zz-voice-urn", URN))[0]
    # a lead with a URL but no identity row, like the ones that predate 070
    keyonly = one("INSERT INTO leads (linkedin_url, name) VALUES (%s, 'ZZ KeyOnly') RETURNING id",
                  (IN + "zz-voice-keyonly",))[0]
    cur.execute("DELETE FROM lead_identities WHERE lead_id=%s", (keyonly,))

    # ── the resolver, at write time ──
    def post(urn, url):
        return one("INSERT INTO posts (post_urn, poster_linkedin_url) VALUES (%s, %s) "
                   "RETURNING poster_company_id, poster_lead_id", (urn, url))

    check("a company-page post resolves to the company", post("zz-p1", "https://www.linkedin.com/company/zz-voice-page/") == (page, None))
    check("a showcase-page post resolves to the showcase row", post("zz-p2", "https://www.linkedin.com/showcase/zz-voice-show") == (show, None))
    check("a page slug matches case-insensitively", post("zz-p3", "https://www.linkedin.com/company/ZZ-Voice-Page") == (page, None))
    check("a vanity-URL post resolves through lead_identities", post("zz-p4", IN + "zz-voice-vanity") == (None, vanity))
    check("a member-URN post resolves through lead_identities", post("zz-p5", IN + URN) == (None, urn_lead))
    check("a lead with no identity row resolves through leads.linkedin_url", post("zz-p6", IN + "zz-voice-keyonly") == (None, keyonly))
    check("an unknown poster stays unresolved", post("zz-p7", IN + "zz-voice-nobody") == (None, None))
    check("a writer-supplied id is kept", one("INSERT INTO posts (post_urn, poster_linkedin_url, poster_lead_id) "
          "VALUES ('zz-p8', %s, %s) RETURNING poster_lead_id", (IN + "zz-voice-nobody", vanity))[0] == vanity)
    refused("a post naming both a company and a lead is refused",
            "INSERT INTO posts (post_urn, poster_company_id, poster_lead_id) VALUES ('zz-p9', %s, %s)", (page, vanity))
    cur.execute("SELECT posts FROM v_posts_unresolved_poster WHERE poster_linkedin_url=%s", (IN + "zz-voice-nobody",))
    check("v_posts_unresolved_poster lists the unknown poster", cur.fetchone() == (1,))

    late = one("INSERT INTO leads (linkedin_url, name) VALUES (%s, 'ZZ Late') RETURNING id", (IN + "zz-voice-nobody",))[0]
    n = one("SELECT resolve_post_posters()")[0]
    check("resolve_post_posters picks up a poster created after the post",
          n >= 1 and one("SELECT poster_lead_id FROM posts WHERE post_urn='zz-p7'")[0] == late, n)

    # ── voice_policy ──
    # 077 seeds an active house-default row per rule-picked group; set them aside so this
    # exercises the table itself (rolled back with everything else).
    cur.execute("UPDATE voice_policy SET superseded_at = now() WHERE superseded_at IS NULL")
    pol = ("INSERT INTO voice_policy (voice_group, version, title_keywords, min_rank, "
           "source, rationale, created_by) VALUES (%s, %s, %s, %s, 'zz source', 'zz why', 'check_voices')")
    state, _ = attempt(pol, ("competitor", "zz-v1", ["vp", "chief"], 90))
    check("a competitor policy is accepted", state is None, state)
    refused("a second active policy for one group collides", pol, ("competitor", "zz-v2", None, 30), code=UNIQUE)
    refused("an unknown voice group is refused", pol, ("bystander", "zz-v1", None, 30))
    refused("a policy without a source is refused",
            "INSERT INTO voice_policy (voice_group, version, rationale, created_by) "
            "VALUES ('industry', 'zz-v1', 'why', 'check_voices')", code="23502")

    # ── voices (targets with purpose social_listening, 087) ──
    ins = ("INSERT INTO targets (company_id, lead_id, target_group, selected_by, policy_version, "
           "purpose, added_by, added_reason) VALUES (%s, %s, %s, %s, %s, 'social_listening', 'check_voices', 'zz test')")
    for args_ in ((page, None, "competitor", "rule", "zz-v1"),
                  (show, None, "own", "manual", None),
                  (None, vanity, "industry", "client_roster", None)):
        state, _ = attempt(ins, args_)
        check(f"a {args_[2]} voice selected by {args_[3]} is accepted", state is None, state)
    refused("a second active voice for the same entity collides", ins, (page, None, "industry", "manual", None), code=UNIQUE)
    refused("a voice naming both a company and a lead is refused", ins, (page, urn_lead, "own", "manual", None))
    refused("a rule-selected voice without a policy version is refused", ins, (None, urn_lead, "competitor", "rule", None))
    refused("a policy version that does not exist is refused", ins, (None, urn_lead, "competitor", "rule", "zz-nope"), code=FK)
    refused("a half-recorded removal is refused",
            "UPDATE targets SET removed_at=now() WHERE company_id=%s AND added_by='check_voices'", (page,))

    cur.execute("SELECT kind, name, canonical_url, posts_held FROM v_voices WHERE added_by='check_voices' ORDER BY kind")
    rows = cur.fetchall()
    check("v_voices shows the three seeded voices with kind, name, URL and posts held", rows == [
        ("company_page", "ZZ Page", "https://www.linkedin.com/company/zz-voice-page", 2),
        ("person", "ZZ Vanity", IN + "zz-voice-vanity", 2),
        ("showcase_page", "ZZ Show", "https://www.linkedin.com/showcase/zz-voice-show", 1),
    ], rows)

    cur.execute("UPDATE targets SET removed_at=now(), removed_by='check_voices', removed_reason='zz' "
                "WHERE company_id=%s AND removed_at IS NULL", (page,))
    check("removing a voice is one row", cur.rowcount == 1)
    state, _ = attempt(ins, (page, None, "industry", "manual", None))
    check("a removed voice can be re-added, and the view shows only the active row",
          state is None and one("SELECT voice_group FROM v_voices WHERE company_id=%s", (page,)) == ("industry",), state)

    # ── 087: one list of who is followed, each row saying what for ──
    tins = ("INSERT INTO targets (company_id, lead_id, target_group, selected_by, purpose, added_by, added_reason) "
            "VALUES (%s, %s, 'competitor', 'manual', %s, 'check_voices', 'zz test')")
    refused("a target that does not say what it is for is refused",
            "INSERT INTO targets (lead_id, target_group, selected_by, added_by, added_reason) "
            "VALUES (%s, 'competitor', 'manual', 'check_voices', 'zz')", (urn_lead,), code="23502")
    refused("an unknown purpose is refused", tins, (None, urn_lead, "bystanding"))
    state, _ = attempt(tins, (page, None, "connection_tracking"))
    check("an entity followed for social listening can also be tracked for connections", state is None, state)
    refused("a second active connection-tracking row for the same entity collides",
            tins, (page, None, "connection_tracking"), code=UNIQUE)
    check("v_voices shows only the social-listening row",
          one("SELECT count(*) FROM v_voices WHERE company_id=%s", (page,)) == (1,))

    return total, fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="Postgres URL. Overrides MARKETBASE_URL.")
    ap.add_argument("--instance", help="Named MarketBase instance (written, then rolled back).")
    args = ap.parse_args()
    conn = _connect(args)
    try:
        total, fails = run(conn)
    finally:
        conn.rollback()
        conn.close()
        print("rolled back: nothing was written")
    if fails:
        print(f"check_voices: {len(fails)} of {total} FAILED: {fails}")
        return 1
    print(f"check_voices: {total} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
