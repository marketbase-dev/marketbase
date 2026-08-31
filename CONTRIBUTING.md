# Contributing to MarketBase

Thanks for helping build this. MarketBase aims to be boring, durable
infrastructure, so the contribution bar leans toward "does this hold up in two
years" rather than "is this clever".

## Ground rules

1. **Migrations are additive and forever.** No DROP, no RENAME, no type
   changes, no removing enum values. See `schema/README.md`.
2. **Provenance is not optional.** Anything that creates a lead must also create
   its `lead_sources` row.
3. **Judgment is versioned.** Qualifications are append-only rows with a
   qualifier name, a version, and a full result. Never a mutable boolean.
4. **Never pay twice.** Any code path that calls a metered API must cache the
   raw response to `enrichment_calls` before parsing, and check that cache first.
5. **No customer data, ever.** Not in fixtures, not in tests, not in issues. Use
   the synthetic seed data in `schema/seed/`.

## Proposing a migration

Open an issue before writing one. The question we will ask is: would a
brand-new MarketBase instance created tomorrow need this change? If the answer
is no, it belongs in application code or a one-shot script, not in the schema.

## Contributing a dataset

Every dataset needs a `manifest.json` recording its source, collection date,
license, and known limitations. Datasets without provenance will not be merged,
which is the same standard we hold user data to.

Datasets must be lawfully collected and redistributable. Do not submit scraped
personal data that you do not have the right to publish.

## Development

```bash
git clone https://github.com/marketbase-dev/marketbase.git
cd marketbase
export MARKETBASE_URL="postgresql://localhost:5432/marketbase_dev"
psql "$MARKETBASE_URL" -f schema/000_bootstrap.sql
for f in schema/0*.sql; do psql "$MARKETBASE_URL" -v ON_ERROR_STOP=1 -f "$f"; done
```

## License

MarketBase is licensed under the [Business Source License 1.1](LICENSE), which
converts to Apache 2.0 four years after each release.

Because MarketBase is dual-licensed (BUSL for everyone, plus commercial licenses
sold by Impact 11, LLC, LLC), we need to hold the rights to your contribution in order to
license it under both. **Submitting a pull request constitutes acceptance of the
[Contributor License Agreement](CLA.md).** It also asks you not to assert
patents against the project and not to sue us over our commercial licensing of
your work. Add your name to `CONTRIBUTORS.md` in your first PR so the record is
explicit. You keep the copyright in your work. You grant us the right to license
it alongside the rest of the project.

This is the same arrangement used by Grafana, Mattermost, and most
dual-licensed projects. If you are not comfortable with it, open an issue
instead and we can implement the idea independently.
