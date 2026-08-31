<h1 align="center">MarketBase</h1>

<p align="center">
  <strong>Your entire GTM context, in one place.</strong>
</p>

<p align="center">
  <a href="https://marketbase.dev">Website</a> ·
  <a href="#quickstart">Quickstart</a> ·
  <a href="#datasets">Datasets</a> ·
  <a href="#claude-code-plugin">Claude Code plugin</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <img alt="License" src="https://img.shields.io/badge/license-BUSL--1.1-blue.svg">
  <img alt="Postgres" src="https://img.shields.io/badge/postgres-14%2B-336791.svg">
  <img alt="Status" src="https://img.shields.io/badge/status-alpha-orange.svg">
</p>

---

## One home for your go-to-market context

GTM data ends up scattered across CSVs, vendor dashboards, and a CRM nobody
wants to pollute. MarketBase gives it one home: a Postgres database you own,
holding every source, every enrichment, and every decision, versioned and
remembered.

**Everything lands here.** Every source, every format, straight in. Vendors,
scrapers, exports, manual research. Stop passing spreadsheets around and stop
treating a CSV as a system of record.

**Your CRM stays clean.** Raw prospect data does not belong in Salesforce. Keep
the mess here, promote only what is real and qualified, and stop flooding your
CRM with records nobody has vetted.

**Never enrich twice.** Every paid API response is cached before it is parsed.
Re-runs, new pipelines, and bug fixes all read from cache. You see exactly what
you spent and on what.

**You own the keys and the bill.** Your Postgres, your API keys, your vendor
contracts, your costs. No platform in the middle marking up your own data back
to you, and nothing to migrate off if you leave.

**Humans and agents work from one database.** Claude Code, Cursor, a teammate in
psql, a nightly cron job. Everyone reads and writes the same context, whatever
harness they use. No agent starts from zero.

**One shared picture.** Competitors, buyers, signals, conversations, and the
reasoning behind every targeting call. All in one place, so the whole team is
working from the same understanding.

## Context, not just records

Storing rows is easy. MarketBase also stores how each lead got here and why you
decided what you decided, which is what makes the context worth sharing across a
team. Four concerns, kept separate.

| Concern | Where it lives | The question it answers |
|---|---|---|
| **Provenance** | `lead_sources` + `source_types` | How did this lead get here? |
| **Judgment** | `lead_qualifications` | Why did we decide to target them, under which version of the rules? |
| **State** | `lead_tags`, `lead_signals` | What is true about them right now? |
| **Outcome** | `campaign_members`, `lead_conversations` | What did we do, and what happened? |

Because judgment is versioned and stored with its full result, you can re-run
last quarter's qualifier and reproduce exactly why a lead was included. Because
provenance is separate from judgment, you can change your targeting rules
without losing the record of where anyone came from.

Two more pieces matter in practice:

- **`enrichment_calls`** caches every raw vendor response. Read-through,
  write-through, keyed on the API, endpoint, and normalized params. You never
  pay twice for the same record, and a job that dies on page 12 of a paginated
  pull resumes from cache for pages 1 through 11.
- **`company_deals`** syncs deal state from your CRM so leads at accounts with
  an open opportunity are suppressed automatically, instead of getting cold
  outbound from a rep who did not know.

## Where it sits in your stack

```
   Data vendors                MarketBase                  Execution
   ─────────────               ──────────                  ─────────
   Apollo        ─┐                                    ┌─  Smartlead
   ZoomInfo      ─┤     ┌──────────────────────┐       ├─  Outreach
   Clay          ─┼───▶ │  provenance          │ ─────▶├─  Lemlist
   LinkedIn      ─┤     │  judgment (versioned)│       ├─  HubSpot
   Your scraper  ─┘     │  state               │       └─  Your CRM
                        │  outcome             │
   CSV upload    ─────▶ │                      │ ◀─────  status written back
                        └──────────────────────┘
```

MarketBase does not send anything. It does not own your sequencer, and it will
not try to replace your CRM. It owns the record of who you targeted and why.

## Quickstart

Requirements: Postgres 14 or newer, and Python 3.10 or newer.

```bash
# 1. Clone
git clone https://github.com/marketbase-dev/marketbase.git
cd marketbase

# 2. Point at a database (any Postgres: local, Neon, Supabase, RDS)
export MARKETBASE_URL="postgresql://user:pass@localhost:5432/marketbase"

# 3. Apply the schema
psql "$MARKETBASE_URL" -f schema/000_bootstrap.sql
for f in schema/0*.sql; do psql "$MARKETBASE_URL" -f "$f"; done

# 4. Load a lead list and see it through the pipeline
psql "$MARKETBASE_URL" -c "SELECT * FROM lead_current_qualification LIMIT 5;"
```

Migrations are additive and safe to re-run. Applied files are tracked in a
`schema_migrations` table, so running the loop twice is a no-op.

### Bring your own CSV

Most people start with a spreadsheet, not a database. That path is supported
first-class: point MarketBase at a CSV and get back a deduplicated list with
provenance attached and suppression already applied.

```bash
marketbase import leads.csv --source apollo --label "Q3 ICP pull"
marketbase qualify --qualifier icp_v1
marketbase export --campaign q3_outbound --format csv
```

You do not need to know what a data warehouse is to use MarketBase. Postgres is
an implementation detail.

## Datasets

MarketBase also publishes open, well-structured GTM datasets under the same
schema, so you can load them straight into your instance.

Each dataset ships with a `manifest.json` recording where it came from, when it
was collected, its license, and its known limitations. Provenance is the whole
point of this project, and that applies to our own data too.

| Dataset | Description | Status |
|---|---|---|
| `source-types` | Canonical taxonomy of GTM lead-discovery methods | Planned |
| `disqualifier-rules` | Reusable exclusion rules (competitors, out of geo, vendor employees) | Planned |
| `campaign-taxonomy` | Naming conventions and status models for outbound campaigns | Planned |

Contributions welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Claude Code plugin

This repository is also a [Claude Code](https://claude.com/claude-code) plugin,
so you can drive MarketBase conversationally.

```bash
# Add this repo as a marketplace, then install the plugin
/plugin marketplace add marketbase-dev/marketbase
/plugin install marketbase@marketbase
```

Once installed:

| Command | What it does |
|---|---|
| `/marketbase:init` | Provision a database and apply the schema |
| `/marketbase:source` | Ingest leads from a vendor or CSV, recording provenance |
| `/marketbase:qualify` | Run a versioned qualifier over a cohort |
| `/marketbase:stage` | Stage qualified leads into a campaign |

The plugin also ships a `marketbase-schema` skill that teaches Claude the schema
and its conventions, and a `qualifier` subagent for running classification at
scale with per-batch durability.

## Project layout

```
marketbase/
├── .claude-plugin/     Plugin + marketplace manifests
├── commands/           Claude Code slash commands
├── skills/             Claude Code skills
├── agents/             Claude Code subagents
├── schema/             SQL migrations (the core of the project)
├── docs/               Landing page, served by GitHub Pages
└── datasets/           Open datasets and their manifests
```

## Roadmap

- [ ] `marketbase` CLI (import, qualify, export)
- [ ] Vendor adapters (Apollo, Clay, LinkedIn, CSV)
- [ ] Sequencer adapters (Smartlead, Outreach, Lemlist)
- [ ] First open datasets published
- [ ] MarketBase Graph: engagement and buying-committee relationships
- [ ] Hosted option for teams who do not want to run Postgres

## Design principles

1. **Provenance is not optional.** Every record knows where it came from.
2. **Judgment is versioned.** A qualification is a decision by a named qualifier
   at a named version, stored with its full result, never a mutable boolean.
3. **Never pay twice.** Every paid API response is cached before it is parsed.
4. **Migrations are additive and forever.** No drops, no renames, no type changes.
5. **MarketBase is the source of truth. Everything else is a consumer.**

## Secrets

MarketBase never hard-codes credentials. Secrets resolve through
[Infisical](https://infisical.com) first, then the environment, then a local
`.env`:

```bash
export INFISICAL_PROJECT_ID=...
export INFISICAL_CLIENT_ID=...        # machine identity
export INFISICAL_CLIENT_SECRET=...
```

Or run any tool under the Infisical CLI, which injects secrets as env vars:

```bash
infisical run --env=prod -- python3 tools/qualify_cohort.py
```

Secrets can be scoped per instance, so `DATABASE_URL` differs between your
MarketBase instances without any code change. See `tools/lib/secrets.py`.

## License

[Business Source License 1.1](LICENSE).

**You can use MarketBase commercially.** Run it inside your own company, run it
for your clients, modify it, self-host it, build on it. That is all expressly
permitted and always free.

**One restriction:** you may not offer MarketBase to third parties as a hosted,
managed, or embedded product whose value derives substantially from what
MarketBase does.

**It becomes Apache 2.0 four years after its release.** Every version converts to a fully
permissive open source license four years after release, and that date is
written into the license itself, not left to our discretion.

See [LICENSING.md](LICENSING.md) for the full map, including relicensing and
proprietary modules.

BUSL is a *source available* license, not an OSI-approved open source license.
We say "source available" rather than "open source" because the distinction is
real and worth being precise about.

Datasets in `datasets/` are licensed separately under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), not BUSL.
