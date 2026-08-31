---
name: marketbase-schema
description: The MarketBase schema and its conventions. Load before writing any SQL, migration, or script against a MarketBase database, or when answering questions about how leads, sources, qualifications, campaigns, or enrichment caching are modeled.
---

# The MarketBase schema

MarketBase is the source of truth for go-to-market data. Everything else, the
data vendors upstream and the sequencer downstream, is a consumer.

## Core model

The schema keeps four concerns separate. Never collapse them.

| Concern | Tables | Mutable? |
|---|---|---|
| Identity | `leads`, `companies` | Yes, enriched over time |
| Provenance | `lead_sources`, `source_types` | Append-only |
| Judgment | `lead_qualifications` | Append-only, versioned |
| State | `lead_tags`, `lead_signals` | Yes |
| Outcome | `campaign_members`, `lead_actions`, `lead_conversations` | Yes, with history |

Supporting tables: `enrichment_calls` (raw vendor response cache),
`company_relationships` (competitor, customer, partner, vendor),
`company_deals` (CRM deal state for suppression), `posts` and
`post_engagements` (engagement graph), `saved_reports`.

## Canonical views

Query these rather than reconstructing the logic:

- `lead_current_qualification` resolves the current verdict per lead
- `v_pending_removals` leads that should be pulled from a campaign
- `v_qualified_no_campaign` qualified but not yet staged
- `v_campaign_funnel` per-campaign funnel counts
- `v_lead_provenance` how each lead was discovered
- `v_leads_at_competitor_company` employed by a flagged competitor
- `v_qualified_held_by_active_deal` suppressed by an open opportunity

## Migration discipline

Migrations are additive and forever. They propagate to every instance.

| Statement | Required form |
|---|---|
| `CREATE TABLE` | `CREATE TABLE IF NOT EXISTS` |
| `CREATE INDEX` | `CREATE INDEX IF NOT EXISTS` |
| `CREATE VIEW` | `CREATE OR REPLACE VIEW` |
| `ADD COLUMN` | `ADD COLUMN IF NOT EXISTS` |
| `CREATE TYPE` | Wrap in `DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL; END $$;` |

Never DROP, RENAME, or change the type of an existing column. Never remove an
enum value. If you need to, add the new shape beside the old one, write to both,
backfill, then remove the old shape in a later migration.

Three-digit zero-padded prefix, snake_case, `.sql`. Always take the next free
number. Never insert between existing ones, because instances that already
applied the later migration would skip the inserted one forever.

**Migrations are never for instance-specific data.** Flagging one company as a
competitor, backfilling one org's leads, or re-qualifying one cohort are
one-shot scripts, not migrations. Ask: would a brand-new instance created
tomorrow need this? If no, it is not a migration.

## Prefer application logic over new columns

When information can be persisted with tables and columns that already exist,
do that instead of adding a migration, even if it costs a little application
code. Each new migration raises the cost of every future change.

## Caching paid API responses

Any script that calls a metered external API must persist the raw response to
`enrichment_calls` before processing it, and must check that cache before
calling.

- Read-through and write-through, keyed on `(api, endpoint, normalized params)`
- Cache the **raw** response, not just the derived rows, so a later stage or a
  re-run after a parsing bug can re-derive without re-fetching
- Paginated pulls cache per page, so a job that dies on page 12 resumes from
  cache for pages 1 through 11

## Durability in batch jobs

1. Flush and commit every 25 records. Never hold results in memory until the end.
2. Idempotency is mandatory. Use a uniqueness key such as
   `(lead_id, qualifier_name, qualifier_version)` and check before insert.
3. External async work needs its tracking ID persisted to disk before the
   submitting call returns.
4. Save runtime decisions, not just final verdicts, so ingest is replayable.

## Tag conventions

Tags use `<category>:<value>`, snake_case, lowercase, no spaces or hyphens.
State tags use the present tense, for example `state:engagers_researched`.
Tags capture mutable classification and are distinct from `lead_sources`
(provenance) and `lead_qualifications` (versioned decisions).
