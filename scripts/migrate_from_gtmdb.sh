#!/usr/bin/env bash
# Register an existing gtmdb database as a MarketBase instance.
#
# There is no data migration. gtmdb and MarketBase are the same schema with the
# same migration filenames, so an existing database is already a valid
# MarketBase instance. This script only wires up the naming and verifies state.
#
# Usage:
#   ./scripts/migrate_from_gtmdb.sh <InstanceName> [path-to-env-file]
#
# Example:
#   ./scripts/migrate_from_gtmdb.sh acme ~/.env.Acme

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

NAME="${1:?usage: migrate_from_gtmdb.sh <InstanceName> [env-file]}"
ENVFILE="${2:-$HOME/.env.${NAME}}"
SLUG=$(printf '%s' "$NAME" | tr '[:upper:] ' '[:lower:]-')

echo "==> Instance: $SLUG"

if [ ! -f "$ENVFILE" ]; then
  echo "No env file at $ENVFILE."
  echo "Pass the path explicitly, or set ${SLUG^^}_MARKETBASE_URL yourself."
  exit 1
fi

URL=$(grep -E '^(GTM_DB_CONNSTRING|DATABASE_URL|MARKETBASE_URL)=' "$ENVFILE" \
      | head -1 | cut -d= -f2- | tr -d '"'"'" || true)
[ -z "$URL" ] && { echo "No connection string found in $ENVFILE"; exit 1; }
echo "==> Found a connection string in $ENVFILE"

echo "==> Verifying the database looks like a MarketBase/gtmdb schema"
MISSING=$(psql "$URL" -tAc "
  SELECT string_agg(t, ', ') FROM unnest(ARRAY[
    'leads','companies','lead_sources','lead_qualifications','campaigns'
  ]) AS t WHERE to_regclass('public.'||t) IS NULL;")
if [ -n "${MISSING// /}" ]; then
  echo "    Missing expected tables: $MISSING"
  echo "    This may not be a gtmdb database. Stopping."
  exit 1
fi
echo "    Core tables present."

APPLIED=$(psql "$URL" -tAc \
  "SELECT count(*) FROM schema_migrations" 2>/dev/null || echo "0")
echo "==> Migrations already recorded: $APPLIED"
echo "    MarketBase uses the same filenames, so these are skipped, not re-run."

echo "==> Registering the instance"
python3 - "$SLUG" <<'PY'
import sys, pathlib
sys.path.insert(0, "tools")
from lib import instances
name = instances.register(sys.argv[1], description="Converted from gtmdb")
print(f"    Registered '{name}' in {instances.config_path()}")
PY

cat <<EOF

==> Done. One manual step remains: store the credential.

    Infisical (recommended):
      secret MARKETBASE_URL at path /$SLUG

    Or environment / .env:
      ${SLUG^^}_MARKETBASE_URL=<the connection string from $ENVFILE>

    Then verify:
      python3 -c "import sys;sys.path.insert(0,'tools');from lib import secrets;\\
        print(bool(secrets.database_url('$SLUG')))"

Nothing was written to your database, and no credential was copied into any
file in this repository.
EOF
