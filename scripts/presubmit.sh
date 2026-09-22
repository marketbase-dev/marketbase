#!/usr/bin/env bash
# MarketBase pre-submit scanner.
#
# Two layers:
#   1. Generic secret + PII patterns (safe to publish, lives in this repo)
#   2. A private denylist of client/person names, read from .sanitize-denylist
#      which is gitignored ON PURPOSE. Putting real client names in a public
#      repo would leak the very thing the list exists to catch.
#
# WORKTREES: because the denylist is gitignored, `git worktree add` does NOT
# copy it, so a worktree had no Layer 2 at all and every commit made from one
# ran generic-patterns-only — silently, since a missing list was a NOTE and a
# pass. Sessions here work in worktrees by default, so that was the normal
# path, not the edge case. The list is now resolved from the main checkout via
# --git-common-dir, and a Layer 2 that cannot be loaded FAILS the scan instead
# of shrugging.
#
# Usage:  ./scripts/presubmit.sh [--staged]
# Install as a git hook:  ln -sf ../../scripts/presubmit.sh .git/hooks/pre-commit

set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1
FAIL=0

if [ "${1:-}" = "--staged" ]; then
  FILES=$(git diff --cached --name-only --diff-filter=ACM)
else
  FILES=$(git ls-files)
fi
[ -z "$FILES" ] && { echo "nothing to scan"; exit 0; }

scan() { # pattern, label
  local hits
  hits=$(printf '%s\n' $FILES | grep -vE '^(LICENSE|LICENSE-APACHE-2\.0|scripts/presubmit\.sh|\.gitignore)$' \
    | xargs -I{} grep -HInEi "$1" {} 2>/dev/null \
    | grep -vEi 'user:pass@|USER:PASSWORD@|<[a-z-]+>:<[a-z-]+>@|youruser:|:password@|example\.com|placeholder|\$\{|<your-|postgres:postgres@localhost|@localhost:5432|@127\.0\.0\.1')
  if [ -n "$hits" ]; then
    echo "BLOCKED: $2"; echo "$hits" | head -12 | sed 's/^/    /'; echo
    FAIL=1
  fi
}

# --- Layer 1: generic patterns, no client info encoded here ---
scan 'sk-[A-Za-z0-9]{16,}'                                   "OpenAI-style API key"
scan 'pat-[A-Za-z0-9-]{16,}'                                 "HubSpot-style token"
scan '(xox[abprs]-[A-Za-z0-9-]{10,})'                        "Slack token"
scan 'AKIA[0-9A-Z]{16}'                                      "AWS access key id"
scan 'ghp_[A-Za-z0-9]{20,}'                                  "GitHub personal access token"
scan '-----BEGIN [A-Z ]*PRIVATE KEY-----'                    "private key"
scan 'postgres(ql)?://[^ "'"'"']*:[^ "'"'"']*@'              "Postgres URL with inline password"
scan '(api|secret|access)_?(key|token)[[:space:]]*[=:][[:space:]]*["'"'"'][A-Za-z0-9_/+-]{16,}' "hardcoded credential"
scan '/Users/[a-z0-9_.-]+/'                                  "local absolute path"
scan '[A-Za-z0-9._%+-]+@(?!example\.(com|org)|user\.noreply)[A-Za-z0-9.-]+\.[A-Za-z]{2,}' "real email address"

# --- Layer 2: private denylist ---
DENY=.sanitize-denylist
# In a linked worktree, .git is a file and --git-common-dir points at the main
# checkout's .git; its parent is the main working tree, where the gitignored
# denylist actually lives.
if [ ! -f "$DENY" ]; then
  COMMON=$(git rev-parse --git-common-dir 2>/dev/null)
  case "$COMMON" in
    /*) MAIN=$(dirname "$COMMON") ;;
    *)  MAIN=$(cd "$(dirname "$COMMON")" 2>/dev/null && pwd) ;;
  esac
  if [ -n "${MAIN:-}" ] && [ -f "$MAIN/$DENY" ]; then
    DENY="$MAIN/$DENY"
    echo "denylist: using $DENY (worktree)"
  fi
fi

if [ -f "$DENY" ]; then
  while IFS= read -r term; do
    [ -z "$term" ] && continue
    case "$term" in \#*) continue ;; esac
    # Letter-only boundaries. Catches Name_TAM_v1 and name_queued, because
    # underscores and digits are not letters, but never matches a name that
    # happens to be a substring of a real word (tatio inside annotations).
    esc=$(printf '%s' "$term" | sed 's/[][\.*^$/]/\\&/g')
    scan "(^|[^A-Za-z])${esc}([^A-Za-z]|$)" "denylisted term: $term"
  done < "$DENY"
else
  # Not a NOTE. This repo is PUBLIC and Layer 2 is the only thing standing
  # between a client name and a permanent public git history, so "I could not
  # load it" must never read the same as "I checked and it was clean".
  echo "BLOCKED: no .sanitize-denylist found (searched this tree and the main"
  echo "         checkout). Layer 2 did NOT run, so client and person names"
  echo "         were NOT checked."
  echo "         cp .sanitize-denylist.example .sanitize-denylist and add your"
  echo "         real terms, or override with: git commit --no-verify"
  echo
  FAIL=1
fi

if [ "$FAIL" -ne 0 ]; then
  echo "Pre-submit scan FAILED. Fix the above, or override with: git commit --no-verify"
  exit 1
fi
echo "Pre-submit scan passed ($(printf '%s\n' $FILES | wc -l | tr -d ' ') files)."
