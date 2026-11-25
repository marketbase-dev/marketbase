---
name: marketbase
description: Initialize and operate MarketBase GTM databases, select instances, ingest leads with provenance, qualify cohorts, and stage campaigns. Use for requests to work with MarketBase.
---

# MarketBase workflows

This skill works in Codex and Claude Code. Resolve the package root as two
levels above this SKILL.md, rather than assuming the current working directory
is the repository. All paths below are relative to that root. Run packaged
tools from the root so their imports and resources resolve.

Read `skills/marketbase-schema/SKILL.md` before database work. Inspect the
actual database schema before constructing SQL; migrations may be newer than
the overview.

Read the document for the requested operation:

- Initialize a database or apply migrations: `commands/init.md`.
- List, register, or switch instances: `commands/instances.md`.
- Ingest a vendor or CSV: `commands/source.md`.
- Qualify a cohort: `commands/qualify.md`.
- Stage qualified leads: `commands/stage.md`.

These documents describe workflows. Claude slash commands are not shell
commands or Codex commands. In Codex, execute the documented workflow directly;
in Claude Code, the existing slash commands remain available. The qualifier
agent in `agents/qualifier.md` is a Claude component, not a registered Codex
agent. Codex can perform qualification directly, keeping the same versioned
verdicts, cache semantics, and durable batches. Stop after three consecutive
failures and report persisted progress rather than retrying indefinitely.

Resolve credentials with `tools/lib/instances.py` and `tools/lib/secrets.py`.
Never print credentials. Claude's `${user_config.database_url}` is optional
Claude-only configuration; Codex uses the environment or instance secrets.
Ask for the database target when no connection resolves. When multiple
instances exist and the user has not selected one, ask before database work.
Installing the plugin does not provision a database.

Use `python3 tools/apply_schema.py --instance <name>` for tracked migrations;
inspect its options before use. The README's proposed import/qualify/export
CLI is roadmap functionality, so inspect `tools/` for implemented runners.
Do not assume a `marketbase` executable exists.

Keep source provenance and versioned judgments intact. Cache paid responses
before parsing, use idempotent writes, and commit every 25 records. Campaign
staging does not send outreach.
