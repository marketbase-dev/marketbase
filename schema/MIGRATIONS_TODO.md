# Bringing the migrations in

`000_bootstrap.sql` is the only migration committed so far. The real schema
lives in your private gtmdb toolkit and has not been copied here yet, on
purpose: this repo is going public, so every file should be reviewed once
before it lands.

To bring them in after review:

```bash
cp ~/.claude/tools/gtmdb/schema/0*.sql schema/
```

Before committing, check each file for:

- Hard-coded customer names, slugs, or domains
- `source_types` rows that only make sense for one client
- Anything under `company_relationships` seeding a specific company
- Internal terminology you would not want in public

Per the project's own rule, migrations should contain nothing instance-specific.
Anything that fails that test belongs in a one-shot script, not here.

Delete this file once the migrations are in.
