#!/usr/bin/env bash
# MarketBase pre-submit scanner.
#
# Two layers:
#   1. Generic secret + PII patterns (safe to publish, lives in this repo)
#   2. A private denylist of client/person names. It is NEVER a file in this
#      repo: putting real client names in a public repo would leak the very
#      thing the list exists to catch.
#
#      SOURCE OF TRUTH: Infisical, secret SANITIZE_DENYLIST in the dedicated
#      "Repo Sanitization" project. One copy, so it cannot drift between a
#      laptop, a second laptop, and CI, and so a new machine inherits it by
#      logging in rather than by someone remembering to hand over a file.
#
#      ITS OWN PROJECT ON PURPOSE. Infisical grants access per PROJECT (roles
#      attach there, narrowable by environment and path, not to one secret), so
#      an identity that could read this list inside a shared project could read
#      every other secret in it too. A CI runner on a PUBLIC repo must not hold
#      a key that also opens vendor API keys and database URLs.
#
#      Resolution order: $SANITIZE_DENYLIST (CI passes it in) ->
#      $SANITIZE_DENYLIST_FILE -> a local .sanitize-denylist if one still
#      exists (legacy, still gitignored) -> Infisical.
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

# Files exempt from the GENERIC patterns. presubmit.sh is here because it
# contains those patterns literally and would match every one of them.
#
# It is NOT exempt from the denylist. The terms live in Infisical, never in this
# file, so scanning it for them is safe -- and necessary: a client name once sat
# in a comment HERE, in the only file the scan could not see, for exactly as
# long as the blanket exemption existed.
EXCLUDE_L1='^(LICENSE|LICENSE-APACHE-2\.0|scripts/presubmit\.sh|\.gitignore)$'
EXCLUDE_L2='^(LICENSE|LICENSE-APACHE-2\.0|\.gitignore)$'
EXCLUDE="$EXCLUDE_L1"

scan() { # pattern, label
  local hits
  hits=$(printf '%s\n' $FILES | grep -vE "$EXCLUDE" \
    | xargs -I{} grep -HInEi "$1" {} 2>/dev/null \
    | grep -vEi 'user:pass@|USER:PASSWORD@|<[a-z-]+>:<[a-z-]+>@|youruser:|:password@|example\.com|placeholder|\$\{|<your-|postgres:postgres@localhost|@localhost:5432|@127\.0\.0\.1')
  if [ -n "$hits" ]; then
    if [ "${REDACT_HITS:-}" = "1" ]; then
      # A denylist hit would otherwise print BOTH the term and the whole
      # matching source line — publishing, into a world-readable CI log, the
      # exact string the denylist exists to keep out of this repo. GitHub masks
      # secret values, but this grep is case-insensitive, so a lower-case
      # spelling in a file never matches the masked literal. Do not rely
      # on the mask: print locations only, and let the developer reproduce it
      # locally where the full detail is safe.
      echo "BLOCKED: ${3:-$2}"
      echo "$hits" | head -12 | sed -E 's/^([^:]+:[0-9]+):.*/    \1/' | sort -u
      echo "    (term and matching text withheld: this log may be public."
      echo "     Reproduce locally with ./scripts/presubmit.sh for full detail.)"
      echo
    else
      echo "BLOCKED: $2"; echo "$hits" | head -12 | sed 's/^/    /'; echo
    fi
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

# CI has no working copy of the gitignored denylist, so it accepts the list
# through the environment instead — $SANITIZE_DENYLIST holds the content, from
# a repository secret. Without this, CI silently ran Layer 1 only for weeks:
# the scan passed every push while checking none of the client names.
if [ -z "${SANITIZE_DENYLIST:-}" ] && [ -n "${SANITIZE_DENYLIST_FILE:-}" ] \
   && [ -f "${SANITIZE_DENYLIST_FILE}" ]; then
  SANITIZE_DENYLIST=$(cat "${SANITIZE_DENYLIST_FILE}")
fi
if [ -n "${SANITIZE_DENYLIST:-}" ] && [ ! -f "$DENY" ]; then
  DENY=$(mktemp)
  printf '%s\n' "$SANITIZE_DENYLIST" > "$DENY"
  trap 'rm -f "$DENY"' EXIT
  # The list arriving by environment means CI, whose logs are public for a
  # public repo. Withhold hit details there by default.
  REDACT_HITS="${REDACT_HITS:-1}"
  echo "denylist: loaded from \$SANITIZE_DENYLIST ($(grep -cvE '^\s*(#|$)' "$DENY") terms)"
  echo "denylist: hit details will be WITHHELD from this log (REDACT_HITS=$REDACT_HITS)"
fi

# Legacy local file, if this machine still has one. Not required any more.
# In a linked worktree, .git is a file and --git-common-dir points at the main
# checkout's .git; its parent is the main working tree.
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

# Last resort and the intended path: pull it from Infisical. Uses whatever
# login this machine already has, so there is nothing extra to distribute.
if [ ! -f "$DENY" ] && command -v infisical >/dev/null 2>&1; then
  _dl=$(infisical secrets get SANITIZE_DENYLIST \
          --projectId "${SANITIZE_PROJECT_ID:-${INFISICAL_TEAM_PROJECT_ID:-d2dbbd59-447f-4fc2-98d2-6ab0acded9fe}}" \
          --env "${INFISICAL_ENV:-dev}" --plain 2>/dev/null)
  if [ -n "$_dl" ]; then
    DENY=$(mktemp)
    printf '%s\n' "$_dl" > "$DENY"
    trap 'rm -f "$DENY"' EXIT
    echo "denylist: loaded from Infisical ($(grep -cvE '^\s*(#|$)' "$DENY") terms)"
  fi
  unset _dl
fi

if [ -f "$DENY" ]; then
  # Layer 2 also reads this file — see EXCLUDE_L2.
  EXCLUDE="$EXCLUDE_L2"
  while IFS= read -r term; do
    [ -z "$term" ] && continue
    case "$term" in \#*) continue ;; esac
    # Letter-only boundaries. Catches Name_TAM_v1 and name_queued, because
    # underscores and digits are not letters, but never matches a name that
    # happens to be a substring of an ordinary word. (The example that used to
    # be written here was itself a real denylisted name -- which is how a
    # client name came to sit in this file. Keep examples synthetic.)
    esc=$(printf '%s' "$term" | sed 's/[][\.*^$/]/\\&/g')
    DENY_N=$((${DENY_N:-0} + 1))
    # Third argument is the REDACTED label — an index, never the term itself.
    scan "(^|[^A-Za-z])${esc}([^A-Za-z]|$)" "denylisted term: $term" \
         "denylisted term #${DENY_N} (name withheld)"
  done < "$DENY"
else
  # Not a NOTE. This repo is PUBLIC and Layer 2 is the only thing standing
  # between a client name and a permanent public git history, so "I could not
  # load it" must never read the same as "I checked and it was clean".
  #
  # The one sanctioned exception is a pull request from a FORK: GitHub does not
  # expose secrets to those runs, so Layer 2 genuinely cannot run and blocking
  # would only punish outside contributors — who do not know our client names
  # anyway. The push-to-main run, which does get the secret, is the gate that
  # matters. Set SANITIZE_DENYLIST_OPTIONAL=1 for exactly that case.
  if [ "${SANITIZE_DENYLIST_OPTIONAL:-}" = "1" ]; then
    echo "NOTE: no denylist available and SANITIZE_DENYLIST_OPTIONAL=1 —"
    echo "      Layer 2 SKIPPED (expected on a fork PR; the push-to-main run"
    echo "      still checks it). Generic patterns ran normally."
    echo
  else
    echo "BLOCKED: could not load the denylist from any source. Layer 2 did"
    echo "         NOT run, so client and person names were NOT checked."
    echo "         Tried: \$SANITIZE_DENYLIST, \$SANITIZE_DENYLIST_FILE, a local"
    echo "         .sanitize-denylist, and Infisical."
    echo "         Locally: run 'infisical login' (secret SANITIZE_DENYLIST in"
    echo "         the 'Repo Sanitization' project)."
    echo "         In CI:   check the Infisical machine-identity auth step."
    echo "         Override with: git commit --no-verify"
    echo
    FAIL=1
  fi
fi

if [ "$FAIL" -ne 0 ]; then
  echo "Pre-submit scan FAILED. Fix the above, or override with: git commit --no-verify"
  exit 1
fi
echo "Pre-submit scan passed ($(printf '%s\n' $FILES | wc -l | tr -d ' ') files)."
