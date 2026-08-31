# MarketBase schema migrations

Each `.sql` file is a migration. They run in filename order and are tracked in
a `schema_migrations` table inside each instance.

## Authoring rules

Migrations are additive and must tolerate being re-run on a database that
already has them applied. The tracking table normally prevents a re-run, but
restores and manual reapplication happen.

| Statement | Required form |
|---|---|
| `CREATE TABLE` | `CREATE TABLE IF NOT EXISTS ...` |
| `CREATE INDEX` | `CREATE INDEX IF NOT EXISTS ...` |
| `CREATE VIEW` | `CREATE OR REPLACE VIEW ...` |
| `ALTER TABLE ... ADD COLUMN` | `ADD COLUMN IF NOT EXISTS ...` |
| `CREATE TYPE` | Wrap in `DO $$ BEGIN ... EXCEPTION WHEN duplicate_object THEN NULL; END $$;` |
| `ALTER TYPE ... ADD VALUE` | `ADD VALUE IF NOT EXISTS` inside a DO block |

Never DROP, RENAME, or change the type of an existing column. Never remove an
enum value. Never rename a table.

If you genuinely need one of those: add the new shape side by side, ship code
that writes to both, backfill, then remove the old shape in a later migration
using `IF EXISTS`.

## Naming

Three-digit zero-padded prefix, snake_case description, `.sql` suffix:

```
008_lead_tags.sql
009_saved_reports.sql
```

The prefix sets run order, so always take the next free number. Never insert a
migration between existing ones, because instances that already applied the
later one would skip the inserted one forever.

## Never for instance-specific data

Migrations are for schema that every instance benefits from equally. Flagging a
particular company as a competitor, backfilling one org's leads, or re-running a
classifier over one cohort are one-shot scripts.
