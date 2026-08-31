---
description: Provision a MarketBase database and apply the schema migrations
argument-hint: "[connection-string | --local | --neon <project-name>]"
---

# marketbase:init

Bootstrap a MarketBase instance and bring its schema up to date.

## Resolve the target database

In order of preference:

1. An explicit connection string passed as an argument.
2. `${user_config.database_url}` from the plugin config.
3. `$MARKETBASE_URL` in the environment.
4. `--local`: create a local Postgres database named `marketbase`.
5. `--neon <project-name>`: provision a Neon project and use its connection URL.

If none resolve, ask the user which they want. Never guess a connection string.

## Apply migrations

```bash
psql "$MARKETBASE_URL" -f schema/000_bootstrap.sql
for f in schema/0*.sql; do
  psql "$MARKETBASE_URL" -v ON_ERROR_STOP=1 -f "$f" || break
done
```

`000_bootstrap.sql` creates the `schema_migrations` tracking table. Every
migration is guarded with `IF NOT EXISTS` and is safe to re-run, so this loop
is idempotent.

## Verify

Confirm the core tables exist and report what was applied:

```sql
SELECT filename, applied_at FROM schema_migrations ORDER BY filename;
```

Then check that `leads`, `companies`, `lead_sources`, `lead_qualifications`,
and `campaigns` are present. Report the count of migrations applied this run
versus already current.

## Write the connection string down

If you provisioned a new database, write the URL to a local `.env` file
(chmod 600) and confirm `.env` is gitignored. Never commit a connection string.

## Instances

This command accepts `--instance <name>` to select which MarketBase database to act on. See `/marketbase:instances`. When more than one instance is configured and the user has not said which, ask rather than guessing.
