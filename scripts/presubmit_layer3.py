#!/usr/bin/env python3
"""Layer 3 of the pre-submit scan: a model pass over the diff.

WHY A THIRD LAYER. Layer 2 matches literal strings from a private denylist, and
that is a floor with a hard ceiling. Three kinds of leak walk straight past it,
all three observed rather than imagined:

  1. A company whose name IS an ordinary English word. Matching is
     letter-boundary, so a short acronym is safe inside a longer word -- but a
     term that stands alone as English hits normal prose every time. On the
     2026-10-10 refresh, suffix-stripping "<Word> Security" off real names produced ordinary words and
     a SQL type name, each of which blocked legitimate text here on the
     first run. ~38 real company names are therefore absent from Layer 2 on
     purpose, including several direct competitors of clients.
  2. A company nobody has added to the list yet -- a prospect from this week, a
     competitor discovered mid-session.
  3. A description that identifies a client without naming it. "the only
     <industry> client we work with" names nobody and
     identifies one company exactly.

Layer 2 cannot see any of those, because none of them is a literal. This layer
asks a model the question a reviewer would ask: does this text identify a real
company, client or person we work with?

FAIL-SAFE DIRECTION, and why it differs from Layer 2. Layer 2 fails CLOSED when
it cannot load its list -- it is the only thing standing between a client name
and a permanent public history, so "I could not check" must never read like "I
checked and it was clean". Layer 3 is different: when it cannot run, Layer 2
still ran, so an unreachable API degrades to a loud warning rather than a block.
A FINDING, though, blocks. The distinction is deliberate: unavailable is not the
same as clean, but neither is it the same as dirty.

Reads the diff from stdin. Exits 0 for clean or unavailable, 1 for a finding.

  ./scripts/presubmit.sh            # runs this automatically when a key exists
  SKIP_LAYER3=1 ./scripts/presubmit.sh
  LAYER3_BLOCK_ON_ERROR=1 ...       # make an unreachable API fail too
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

MODEL = os.environ.get("LAYER3_MODEL", "claude-haiku-4-5-20251001")
MAX_DIFF_BYTES = int(os.environ.get("LAYER3_MAX_DIFF_BYTES", "180000"))
API = "https://api.anthropic.com/v1/messages"

PROMPT = """You are the last gate before a code change lands in a PUBLIC git \
repository owned by a GTM consultancy. Public means permanent: once pushed, a \
name cannot be unpublished.

Your job is to find text that would reveal WHO THE CONSULTANCY WORKS WITH -- \
its clients, their competitors, its prospects, or named individuals at any of \
them. That association is the confidential thing, not the companies' existence.

BLOCK on any of these:
- a real company named in a way that ties it to this consultancy's work: a \
client, a client's competitor, a prospect, a tracked account, an example drawn \
from real client data
- a real person's name tied to a company or to the consultancy's work
- a description specific enough to identify one real company without naming it \
("the client whose own tooling rejects most of our leads")
- a real company name used as an EXAMPLE in a comment, docstring or test \
fixture, even an innocuous-looking one -- this is the most common leak by far
- an identifier carrying a real name: a file path, workspace id, database name, \
Slack channel, email address, LinkedIn slug

Do NOT block on:
- vendors and APIs the code calls: LinkedIn, Clay, Anthropic, OpenAI, Supabase, \
Neon, Postgres, GitHub, Infisical, RapidAPI, Apollo, HubSpot, Salesforce, n8n, \
Cloudflare, AWS. Naming an integration is not naming a client.
- the consultancy's own name and its product names
- obviously synthetic placeholders: Acme, Foo, Northwind, Contoso, Example Corp, \
<Client>, {client}
- generic industry words, job titles, technology terms
- a company name that is also an ordinary English word used AS that ordinary \
word (e.g. "adaptive" in "adaptive threshold", "push" in "push the branch")

These names are known to be real and sensitive, and they are ABSENT from the \
literal denylist precisely because each is an ordinary word or too short to \
match safely. Judge them in context -- flag the company, ignore the word:
@@WATCHLIST@@

Here is the change under review. Only judge ADDED lines (prefixed +).

@@DIFF@@

Reply with JSON only, no prose:
{"verdict": "clean" | "block",
 "findings": [{"file": "<path>", "line_hint": "<a few words of the offending \
text, at most 8>", "why": "<one short sentence>"}]}

An empty findings list with verdict "clean" is the expected answer for most \
changes. Do not invent findings to seem useful; a false block costs a human \
their time and teaches them to pass --no-verify."""


def _watchlist() -> str:
    """Names Layer 2 cannot carry. Kept beside the denylist, never in this repo."""
    for src in (os.environ.get("LAYER3_WATCHLIST_FILE"),
                os.path.expanduser("~/.claude/.layer3-watchlist")):
        if src and os.path.exists(src):
            terms = [l.strip() for l in open(src) if l.strip()
                     and not l.strip().startswith("#")]
            if terms:
                return ", ".join(terms)
    inline = os.environ.get("LAYER3_WATCHLIST", "").strip()
    if inline:
        return inline
    # Absent is not fatal: the model still judges the diff on its own terms,
    # it just loses the focused hint for the ordinary-word names.
    return "(no watch list available — judge on context alone)"


def _api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    # Same resolution the rest of the toolchain uses, so a developer who has
    # logged into Infisical needs nothing extra.
    try:
        r = subprocess.run(
            ["infisical", "secrets", "get", "ANTHROPIC_API_KEY",
             "--projectId", os.environ.get(
                 "INFISICAL_TEAM_PROJECT_ID",
                 "5669d398-39d6-41d9-80d0-8c325ac819b7"),
             "--env", os.environ.get("INFISICAL_ENV", "dev"),
             "--plain", "--silent"],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _unavailable(why: str) -> int:
    """Layer 3 could not run. Loud, but not a block -- Layer 2 still ran."""
    print(f"NOTE: Layer 3 (model pass) did not run: {why}")
    print("      Layer 2 ran normally, so literal denylist terms WERE checked.")
    print("      A name that is an ordinary English word, or one nobody has")
    print("      added to the list yet, was NOT checked.")
    if os.environ.get("LAYER3_BLOCK_ON_ERROR") == "1":
        print("      LAYER3_BLOCK_ON_ERROR=1 — failing the scan.")
        return 1
    print()
    return 0


def main() -> int:
    if os.environ.get("SKIP_LAYER3") == "1":
        return _unavailable("SKIP_LAYER3=1")

    diff = sys.stdin.read()
    added = [l for l in diff.splitlines()
             if l.startswith("+") and not l.startswith("+++")]
    if not added:
        return 0
    if len(diff) > MAX_DIFF_BYTES:
        # Truncate rather than skip: the first N bytes of a huge diff is still
        # worth judging, and saying so beats silently checking nothing.
        print(f"NOTE: Layer 3 truncated the diff at {MAX_DIFF_BYTES} bytes "
              f"(was {len(diff)}).")
        diff = diff[:MAX_DIFF_BYTES]

    key = _api_key()
    if not key:
        return _unavailable("no ANTHROPIC_API_KEY (env or Infisical)")

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 1024,
        # Sentinels, not %-formatting or .format(): this prompt contains a
        # literal "90%", JSON braces and a {client} placeholder, and every
        # templating mechanism in the language chokes on one of those.
        "messages": [{"role": "user", "content":
            PROMPT.replace("@@WATCHLIST@@", _watchlist())
                  .replace("@@DIFF@@", diff)}],
    }).encode()
    req = urllib.request.Request(API, data=body, headers={
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        return _unavailable(f"API call failed ({type(e).__name__})")
    except Exception as e:  # noqa: BLE001 - never let this layer break a commit
        return _unavailable(f"unexpected error ({type(e).__name__})")

    text = "".join(b.get("text", "") for b in payload.get("content", []))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return _unavailable("model returned no JSON")
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError:
        return _unavailable("model returned unparseable JSON")

    findings = out.get("findings") or []
    if out.get("verdict") != "block" or not findings:
        print(f"Layer 3 (model pass, {MODEL}): clean.")
        return 0

    # Same redaction rule as Layer 2: a CI log may be public, and printing the
    # offending text there would publish the exact string we are blocking.
    redact = os.environ.get("REDACT_HITS") == "1"
    print("BLOCKED: Layer 3 (model pass) flagged a possible client/company leak")
    for f in findings[:12]:
        loc = f.get("file") or "?"
        if redact:
            print(f"    {loc}")
        else:
            print(f"    {loc}: {f.get('line_hint','?')}  — {f.get('why','')}")
    if redact:
        print("    (text and reasons withheld: this log may be public.")
        print("     Reproduce locally with ./scripts/presubmit.sh for detail.)")
    print("    This layer is a judgement, not a literal match — if it is wrong,")
    print("    commit with --no-verify and report it, so the prompt improves.")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
