---
description: List, add, or switch between MarketBase instances
argument-hint: "[list | add <name> | use <name>]"
---

# marketbase:instances

Most people run one MarketBase. Agencies and consultancies run one per client.
An instance is a name mapped to a Postgres database. Same schema, same tools.

## list

Read `~/.marketbase/instances.json` and show each instance, its description, and
which is the default. For each, report whether its credential actually resolves
(without printing the credential), so a misconfigured instance is obvious.

## add `<name>`

Register the instance, then confirm where its connection string should live:

| Where secrets live | Set this |
|---|---|
| Infisical | secret `MARKETBASE_URL` at path `/<name>` |
| Environment | `<NAME>_MARKETBASE_URL` |
| `.env` | `<NAME>_MARKETBASE_URL` |

Names are lowercased, with spaces becoming hyphens. Never write a connection
string into `instances.json`. That file holds names and metadata only.

Offer to run `/marketbase:init --instance <name>` afterward.

## use `<name>`

Set `default_instance`. Confirm the change and remind the user that
`MARKETBASE_INSTANCE` in the environment overrides it for a single shell.

## How resolution works

1. `--instance <name>` on the command
2. `$MARKETBASE_INSTANCE`
3. `default_instance` in the config file
4. the literal name `default`

Every MarketBase command accepts `--instance`. When the user has more than one
instance and has not said which, ask rather than guessing. Writing a cohort into
the wrong client's database is expensive to undo.
