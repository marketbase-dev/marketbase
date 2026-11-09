#!/usr/bin/env python3
"""Exercise the post key rule, reposts, one reaction per person per post and the post-listing
cache policy (schema/088, 089, 090) against a database that already has every migration applied.
Everything runs inside ONE transaction that is always rolled back.

Why this exists: a post was stored under whichever of its two LinkedIn ids a
vendor handed over, so one post could become two rows and a repost could pose
as its original. The rule lives in SQL, invisible to the Python checks: this
proves the database refuses a key that is not the number in the post's own
link, accepts both honest labels, and links a repost to its original.

Usage:
  python3 scripts/check_post_keys.py                 # MARKETBASE_URL (CI)
  python3 scripts/check_post_keys.py --url postgresql://...
  python3 scripts/check_post_keys.py --instance <name>
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

CHECK, FK = "23514", "23503"
FEED = "https://www.linkedin.com/feed/update/"
A1, A2, A3 = "7300000000000000001", "7300000000000000002", "7300000000000000003"
S1 = "7299999999999999001"


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
        cur.execute("SAVEPOINT k")
        try:
            cur.execute(sql, params)
            row = cur.fetchone() if cur.description else None
            cur.execute("RELEASE SAVEPOINT k")
            return None, row
        except Exception as e:  # noqa: BLE001 - the error IS the result
            cur.execute("ROLLBACK TO SAVEPOINT k")
            return _sqlstate(e), None

    ins = "INSERT INTO posts (post_urn, post_url) VALUES (%s, %s) RETURNING id"

    # ── the key ──
    state, orig = attempt(ins, (f"urn:li:activity:{A1}", f"{FEED}urn:li:activity:{A1}/"))
    check("a post keyed on the activity id in its own link is accepted", state is None, state)
    state, _ = attempt(ins, (f"urn:li:activity:{A3}", f"https://www.linkedin.com/posts/zz-name_text-activity-{A3}-AbCd"))
    check("the slug form of the link is accepted", state is None, state)
    state, _ = attempt(ins, (f"urn:li:ugcPost:{S1}", f"{FEED}urn:li:ugcPost:{S1}"))
    check("an original known only by its content id, labelled ugcPost, is accepted", state is None, state)
    state, _ = attempt(ins, (f"urn:li:activity:{S1}", f"{FEED}urn:li:activity:{A2}"))
    check("a content id under the activity label, beside the real link, is refused", state == CHECK, state)
    state, _ = attempt(ins, ("urn:li:activity:zz-fixture", None))
    check("a key that is not a LinkedIn id is refused", state == CHECK, state)
    state, _ = attempt(ins, (A2, None))
    check("a bare number with no label is refused", state == CHECK, state)
    state, _ = attempt("UPDATE posts SET post_url = %s WHERE id = %s",
                       (f"{FEED}urn:li:activity:{A2}", orig[0]))
    check("changing a post's link to another post's is refused", state == CHECK, state)
    state, _ = attempt("UPDATE posts SET likes = 5 WHERE id = %s", (orig[0],))
    check("a likes refresh does not touch the key rule", state is None, state)

    # ── reposts ──
    state, rp = attempt("INSERT INTO posts (post_urn, post_url, post_text, reposted_post_id) "
                        "VALUES (%s, %s, %s, %s) RETURNING id",
                        (f"urn:li:activity:{A2}", f"{FEED}urn:li:activity:{A2}", "zz added text", orig[0]))
    check("a repost points at its original", state is None, state)
    state, _ = attempt("UPDATE posts SET reposted_post_id = id WHERE id = %s", (rp[0] if rp else orig[0],))
    check("a post cannot repost itself", state == CHECK, state)
    state, _ = attempt("INSERT INTO posts (post_urn, reposted_post_id) VALUES (%s, gen_random_uuid())",
                       ("urn:li:activity:7300000000000000009",))
    check("a repost of a post that does not exist is refused", state == FK, state)

    # ── 089: the key rule is a CHECK on every row; one reaction per person per post ──
    cur.execute("SELECT count(*) FROM pg_constraint WHERE conname = 'posts_key_matches_link' AND convalidated")
    check("the post key rule is a validated CHECK on every row", cur.fetchone()[0] == 1)
    lead = attempt("INSERT INTO leads (linkedin_url, name) VALUES (%s, 'ZZ Reactor') RETURNING id",
                   ("https://www.linkedin.com/in/zz-post-keys-reactor",))[1][0]
    other = attempt("INSERT INTO leads (linkedin_url, name) VALUES (%s, 'ZZ Other') RETURNING id",
                    ("https://www.linkedin.com/in/zz-post-keys-other",))[1][0]
    rx = "INSERT INTO post_engagements (post_id, lead_id, engagement_type, merged_from_lead_id) VALUES (%s, %s, %s, %s)"
    state, _ = attempt(rx, (orig[0], lead, "reaction", None))
    check("a reaction is accepted", state is None, state)
    state, _ = attempt(rx, (orig[0], lead, "reaction", None))
    check("a second reaction by the same person on the same post is refused (090)", state == "23505", state)
    state, _ = attempt(rx, (orig[0], lead, "reaction", other))
    check("a reaction a lead merge moved onto the keeper sits beside the keeper's own (090)", state is None, state)
    state, _ = attempt(rx, (orig[0], lead, "comment", None))
    state2, _ = attempt(rx, (orig[0], lead, "comment", None))
    check("a person may comment more than once", state is None and state2 is None, (state, state2))
    state, _ = attempt(rx, (orig[0], lead, "repost", None))
    check("a plain repost is recorded as a 'repost' engagement", state is None, state)
    cur.execute("SELECT count(*) FROM source_types WHERE name = 'people_sweep'")
    check("the people_sweep source type exists", cur.fetchone()[0] == 1)

    # ── post listings never expire ──
    cur.execute("""SELECT api, endpoint FROM enrichment_source_policy
                    WHERE fresh_days IS NULL AND (api, endpoint) IN
                          (('saleleads', '/api/v1/user/posts'), ('saleleads', '/api/v1/company/posts'),
                           ('fresh-linkedin-profile-data', '/get-profile-posts'),
                           ('fresh-linkedin-profile-data', '/get-company-posts'))""")
    check("the four post-listing endpoints never expire", len(cur.fetchall()) == 4)
    return total, fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--instance")
    args = ap.parse_args()
    conn = _connect(args)
    try:
        total, fails = run(conn)
    finally:
        conn.rollback()
        conn.close()
    print(f"check_post_keys: {total - len(fails)}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
