#!/usr/bin/env python3
"""Glossary check: fail any text being written that uses a retired term.

The canonical list is the **Retired terms** table in `~/.claude/GLOSSARY.md`.
Public repos cannot read that file (it names clients elsewhere), so they carry
an export, `scripts/glossary-retired.json`, written by `--export` and asserted
fresh by `--check-export` wherever the glossary itself is present.

What is scanned is **lines being added**, never the whole file: history that
names a retired term stands, and the old code is rewritten ticket by ticket.
A line that has to contain a retired term (the migration that renames it, a
note explaining the old name) carries the token `glossary-ok`.

Modes:
  --text FILE | -          scan this text (the hook feeds Edit/Write content)
  --staged                 scan lines added in the git index
  --diff BASE              scan lines added since BASE (a ref or a SHA)
  --repo                   pick BASE for the current checkout: the merge base
                           with origin/main on a branch, HEAD~1 on main
  --export OUT.json        write the retired-terms table as JSON
  --check-export OUT.json  fail if OUT.json differs from the table
  --self-test              prove the matcher catches and spares what it should

Exit 0 clean, 1 hits (listed on stdout), 2 misuse.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

GLOSSARY = Path(os.environ.get("GLOSSARY_MD", Path.home() / ".claude" / "GLOSSARY.md"))
OK_TOKEN = "glossary-ok"
# Paths whose job is to hold history or the list itself.
EXEMPT = re.compile(r"(^|/)(GLOSSARY\.md|glossary-retired\.json|check_glossary\.py|"
                    r"require-glossary-terms\.py|memory/[^/]+\.md|CHANGELOG[^/]*)$")


def load_terms(source: Path | None = None) -> list[dict]:
    """Rows of {pattern, use_instead, retired}. From the JSON export when given
    one, otherwise parsed out of the glossary's Retired terms table."""
    if source and source.suffix == ".json":
        return json.loads(source.read_text())
    md = (source or GLOSSARY).read_text()
    tail = md.split("## Retired terms", 1)
    if len(tail) != 2:
        raise SystemExit("GLOSSARY.md has no '## Retired terms' section")
    rows = []
    for line in tail[1].splitlines():
        if not line.startswith("|") or set(line.strip()) <= set("|-"):
            continue
        # Split on ' | ' (space-pipe-space): a regex alternation like (db|database)
        # has no spaces around its bar, the table's column separators do.
        cells = [c.strip() for c in re.split(r"\s\|\s", line.strip().strip("|"))]
        if len(cells) < 3 or cells[0] == "pattern":
            continue
        rows.append({"pattern": cells[0].strip("`"), "use_instead": cells[1],
                     "retired": cells[2]})
    if not rows:
        raise SystemExit("GLOSSARY.md: Retired terms table is empty")
    return rows


def compile_terms(rows):
    return [(re.compile(r["pattern"], re.I), r) for r in rows]


def scan_lines(lines, terms, path="<text>", offset=1):
    """(path, lineno, matched text, use_instead) for every retired term on a
    line that does not carry the ok token."""
    hits = []
    for i, line in enumerate(lines, offset):
        if OK_TOKEN in line:
            continue
        for rx, row in terms:
            m = rx.search(line)
            if m:
                hits.append((path, i, m.group(0), row["use_instead"]))
    return hits


def added_lines(diff_text):
    """{path: [(lineno, line)]} of '+' lines from a unified diff with -U0."""
    out, path, lineno = {}, None, 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            path = path[2:] if path.startswith("b/") else path
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) if m else 1
        elif raw.startswith("+") and not raw.startswith("+++") and path:
            if path != "/dev/null" and not EXEMPT.search(path):
                out.setdefault(path, []).append((lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            pass
    return out


def scan_diff(diff_text, terms):
    hits = []
    for path, lines in added_lines(diff_text).items():
        for lineno, line in lines:
            hits += scan_lines([line], terms, path, lineno)
    return hits


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def repo_base():
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch in ("main", "master", "HEAD"):
        return "HEAD~1"
    try:
        return git("merge-base", "origin/main", "HEAD").strip()
    except subprocess.CalledProcessError:
        return "HEAD~1"


def report(hits) -> int:
    if not hits:
        print("glossary: ok")
        return 0
    print(f"glossary: {len(hits)} retired term(s) in lines being added")
    for path, lineno, text, use in hits[:40]:
        print(f"  {path}:{lineno}: '{text}' -> use {use}")
    print(f"  (a line that must say this carries the token '{OK_TOKEN}'; the list is "
          f"the Retired terms table in GLOSSARY.md)")
    return 1


def self_test(terms) -> int:
    should_hit = ["the lead DB has it", "lead-database rows", "gtmdb track",
                  "tag = 'state:removed_from_monitoring'", "excluded:sdr_bdr",
                  "excluded_target", "reason manual_removal", "removed from monitoring",
                  "kick_connection_send"]
    should_pass = ["MarketBase holds it", "ignored:comp_intel_target",
                   "excluded_role:investor", "leading database vendor",
                   "state:removed_from_monitoring glossary-ok", "kick_connection_worker",
                   "exclude_targets()", "lead_db_track"]
    bad = [s for s in should_hit if not scan_lines([s], terms)]
    bad += [s for s in should_pass if scan_lines([s], terms)]
    if bad:
        print("glossary self-test FAILED on: " + "; ".join(repr(b) for b in bad))
        return 1
    print(f"glossary self-test ok ({len(terms)} terms)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--terms", type=Path, help="GLOSSARY.md or a glossary-retired.json "
                    "(default: ~/.claude/GLOSSARY.md, else ./scripts/glossary-retired.json)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="file to scan, or - for stdin")
    g.add_argument("--staged", action="store_true")
    g.add_argument("--diff", metavar="BASE")
    g.add_argument("--repo", action="store_true")
    g.add_argument("--export", type=Path, metavar="OUT.json")
    g.add_argument("--check-export", type=Path, metavar="OUT.json")
    g.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    source = a.terms
    if source is None and not GLOSSARY.exists():
        local = Path("scripts/glossary-retired.json")
        if not local.exists():
            print("glossary: no ~/.claude/GLOSSARY.md and no scripts/glossary-retired.json")
            return 2
        source = local
    rows = load_terms(source)
    terms = compile_terms(rows)

    if a.export:
        a.export.write_text(json.dumps(rows, indent=2) + "\n")
        print(f"glossary: exported {len(rows)} terms to {a.export}")
        return 0
    if a.check_export:
        if not GLOSSARY.exists():
            print("glossary: canonical GLOSSARY.md absent here, export not compared")
            return 0
        if json.loads(a.check_export.read_text()) != rows:
            print(f"glossary: {a.check_export} is STALE; run --export {a.check_export}")
            return 1
        print("glossary: export is current")
        return 0
    if a.self_test:
        return self_test(terms)
    if a.text:
        text = sys.stdin.read() if a.text == "-" else Path(a.text).read_text()
        return report(scan_lines(text.splitlines(), terms, a.text))
    if a.staged:
        return report(scan_diff(git("diff", "--cached", "-U0", "--no-color"), terms))
    base = a.diff or (repo_base() if a.repo else None)
    return report(scan_diff(git("diff", "-U0", "--no-color", f"{base}...HEAD")
                            if a.repo and base != "HEAD~1"
                            else git("diff", "-U0", "--no-color", base, "HEAD"), terms))


if __name__ == "__main__":
    sys.exit(main())
