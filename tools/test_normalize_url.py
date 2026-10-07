#!/usr/bin/env python3
"""normalize_linkedin_url: the percent-encoding cases, and the limits.

Run: python3 tools/test_normalize_url.py

Every COLLAPSE case below is a real duplicate measured on 2026-10-07 across two
MarketBase instances: 74 duplicate groups / 75 extra lead rows in one, 9 groups
/ 10 extra rows in the other, and 22 people being monitored twice because the
same human sat behind two linkedin_url strings.

The DISTINCT cases matter as much. A normaliser that over-collapses invents a
merge, and merging two different people is worse than carrying a duplicate.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import normalize_linkedin_url as N

B = "https://www.linkedin.com/in/"

# (label, a, b) -- must normalise to the SAME key.
#
# Slugs here are SYNTHETIC. The duplicates these came from are real sellers in
# client databases, and this file is public; what the test needs is the byte
# class (2-, 3-, 4-byte UTF-8, and hex case), never the person.
COLLAPSE = [
    # 2-byte: hex case differing on the same byte. Three rows for one seller;
    # no string tidy collapses these, only decoding.
    ("hex case upper vs lower", B + "qa-n%C3%B1-01", B + "qa-n%c3%b1-01"),
    ("2-byte encoded vs literal", B + "qa-n%C3%B1-01", B + "qa-n\u00f1-01"),
    ("2-byte accent", B + "qa-%C3%A9%C3%A8-02", B + "qa-\u00e9\u00e8-02"),
    # 4-byte: an emoji pasted into a display name, which is how a whole class
    # of these arose -- the slug is generated from the name.
    ("4-byte encoded vs literal", B + "qa-%F0%9F%9A%80-03", B + "qa-\U0001F680-03"),
    ("4-byte hex case",           B + "qa-%F0%9F%9A%80-03", B + "qa-%f0%9f%9a%80-03"),
    # 3-byte: trademark / registered marks, and CJK, which showed up as badly
    # as the accents -- non-Latin-script names are hit systematically.
    ("3-byte trademark", B + "qa-%E2%84%A2-04", B + "qa-\u2122-04"),
    ("3-byte registered", B + "qa-%C2%AE-05", B + "qa-\u00ae-05"),
    ("3-byte CJK", B + "qa-%E9%AB%98%E6%A9%8B-06", B + "qa-\u9ad8\u6a4b-06"),
    # Idempotence: normalising an already-normalised key must not move it.
    ("idempotent on decoded", B + "qa-n\u00f1-01", N(B + "qa-n\u00f1-01")),
    ("idempotent on encoded", N(B + "qa-n%C3%B1-01"), N(N(B + "qa-n%C3%B1-01"))),
    # Double-encoded: the loop is what makes this stable rather than
    # one-pass-different-every-time.
    ("double encoded settles", N(B + "a%2520b"), N(N(B + "a%2520b"))),
    # Pre-existing behaviour must survive the change.
    ("host case + scheme", "HTTP://LinkedIn.com/in/qa-plain", B + "qa-plain"),
    ("trailing slash",     B + "qa-plain/",                   B + "qa-plain"),
    ("query + fragment",   B + "qa-plain?trk=x#about",         B + "qa-plain"),
]

# (label, a, b) -- must stay DISTINCT.
DISTINCT = [
    # THE constraint from the review: member-URN case is significant. These
    # two rows are almost certainly one person, but the lowercased form 404s
    # against saleleads/Fresh, so the normaliser must NOT fuse them -- the
    # merge tool has to flag the pair and keep the case-preserved row.
    # If this test ever fails, a lowercased URN can silently become the
    # surviving identity of a live monitored target.
    # (URN below is synthetic, URN-SHAPED: a real one identifies a real person.)
    ("URN case is significant",
     B + "ACwAAQqExampleUrnShapedValue000000000",
     B + "acwaaqqexampleurnshapedvalue000000000"),
    # Vanity handle case, same reason.
    ("handle case preserved", B + "Qa-Plain", B + "qa-plain"),
    # Different people whose slugs differ only past the decode.
    ("different people", B + "qa-n%C3%B1-01", B + "qa-n%C3%B1-02"),
]


def main() -> int:
    bad = []
    for label, a, b in COLLAPSE:
        na, nb = N(a), N(b)
        if na != nb:
            bad.append(f"  COLLAPSE {label}: {na!r} != {nb!r}")
    for label, a, b in DISTINCT:
        na, nb = N(a), N(b)
        if na == nb:
            bad.append(f"  DISTINCT {label}: both became {na!r}")

    # A decoded key must never contain a stray escape left half-done.
    leftover = N(B + "qa-%F0%9F%9A%80-03")
    if "%" in leftover:
        bad.append(f"  leftover escape in {leftover!r}")

    total = len(COLLAPSE) + len(DISTINCT) + 1
    if bad:
        print(f"FAIL {len(bad)}/{total}")
        print("\n".join(bad))
        return 1
    print(f"ok {total} cases ({len(COLLAPSE)} collapse, {len(DISTINCT)} distinct)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
