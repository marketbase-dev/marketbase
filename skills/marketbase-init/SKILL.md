---
name: marketbase-init
description: Provision a MarketBase database and apply the schema migrations. Use when initializing a new MarketBase instance or bringing an existing one up to date.
---

# marketbase-init

Bootstrap a MarketBase instance and bring its schema up to date.

## Resolve the target database

In order of preference:

1. An explicit connection string passed by the user.
2. `$MARKETBASE_URL` (or `<INSTANCE>_MARKETBASE_URL`) in the environment or `.env`.
3. A local Postgres database named `marketbase`.
4. A Neon project connection URL.

If none resolve, ask the user which they want. Never guess a connection string.

## Apply migrations

Run from the plugin/repository root (two levels above this `SKILL.md`). Install `psycopg[binary]` in your Python environment if neither `psycopg` nor `psycopg2` is available.

```bash
python3 tools/apply_schema.py
# Or select a registered instance:
python3 tools/apply_schema.py --instance <name>
```

The runner records applied files in `schema_migrations`, skips completed migrations, and stops on errors. Use this tracked runner rather than replaying every SQL file manually. Inspect pending migrations before changing an existing instance.

## Verify

Confirm the core tables exist and report what was applied:

```sql
SELECT filename, applied_at FROM schema_migrations ORDER BY filename;
```

Then check that `leads`, `companies`, `lead_sources`, `lead_qualifications`, and `campaigns` are present. Report the count of migrations applied this run versus already current.

## Write the connection string down

If you provisioned a new database, write the URL to a local `.env` file (`chmod 600`) and confirm `.env` is gitignored. Never commit a connection string.

## Instances

This workflow accepts `--instance <name>` to select which MarketBase database to act on. See the `marketbase-instances` skill. When more than one instance is configured and the user has not said which, ask rather than guessing.
