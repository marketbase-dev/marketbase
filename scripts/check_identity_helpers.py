#!/usr/bin/env python3
"""Pin the meaning of the LinkedIn identity helpers in tools/lib.

Why this exists: `lib.linkedin_urn()` spent months returning the vanity slug
(whatever follows `/in/`), and eight tools wrote that slug into
`leads.linkedin_urn` -- 42,208 non-URN values across the instances. The helper
is now `linkedin_handle()`, named for what it does, and the member token has
its own helper, `member_urn_token()`. This test keeps the two from drifting
back together: a rename that quietly regained the old meaning would pass every
other check in the repo.

Usage:
  python3 scripts/check_identity_helpers.py --self-test
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import lib  # noqa: E402

VANITY = "https://www.linkedin.com/in/jane-doe-example"
URN_URL = "https://www.linkedin.com/in/" + "ACoAA" + "TEST" * 8 + "01"
# Shaped like a member URN (39 chars, ACoAA / ACwAA prefix), deliberately low
# entropy so a secret scanner has nothing to find.
TOKEN = "ACoAA" + "TEST" * 8 + "01"
SEARCH = "ACwAA" + "TEST" * 8 + "01"


def self_test() -> int:
    checks = [
        ("linkedin_handle returns the vanity slug",
         lib.linkedin_handle(VANITY) == "jane-doe-example"),
        ("linkedin_handle tolerates a trailing slash",
         lib.linkedin_handle(VANITY + "/") == "jane-doe-example"),
        ("linkedin_handle of None is the empty string",
         lib.linkedin_handle(None) == ""),
        ("linkedin_handle is NOT a URN extractor: a vanity URL yields no AC token",
         not lib.member_urn_token(lib.linkedin_handle(VANITY))),
        ("member_urn_token finds the token in a URN-form URL",
         lib.member_urn_token(URN_URL) == TOKEN),
        ("member_urn_token finds the token in a bare urn:li string",
         lib.member_urn_token(f"urn:li:fsd_profile:{TOKEN}") == TOKEN),
        ("member_urn_token returns None for a vanity URL",
         lib.member_urn_token(VANITY) is None),
        ("member_urn_token scans several values and takes the first hit",
         lib.member_urn_token(VANITY, None, URN_URL) == TOKEN),
        ("member_urn_token accepts the search namespace too",
         lib.member_urn_token(SEARCH) == SEARCH),
        ("the old name is gone, so it cannot regain the old meaning",
         not hasattr(lib, "linkedin_urn")),
    ]
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if failed:
        print(f"check_identity_helpers: {len(failed)} of {len(checks)} failed", file=sys.stderr)
        return 1
    print(f"check_identity_helpers: {len(checks)} checks passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true", help="run the pinned checks")
    args = ap.parse_args()
    if not args.self_test:
        ap.print_help()
        return 2
    return self_test()


if __name__ == "__main__":
    sys.exit(main())
