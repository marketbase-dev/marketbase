---
description: Ingest leads from a vendor or CSV into MarketBase, recording full provenance
argument-hint: "<csv-path | vendor> [--source-type <type>] [--label <text>]"
---

# marketbase:source

Bring leads into MarketBase with their provenance intact.

## The rule that matters

Never insert a lead without a matching row in `lead_sources`. Provenance is the
point of this project. A lead with no source is a lead nobody can defend later.

`lead_sources` records **how the lead was discovered**. It is not the same as
`company_relationships`, which records **what a company is to you** (competitor,
customer, partner). Do not conflate them.

## Steps

1. **Validate the source type.** Check the requested `source_type` against the
   `source_types` registry. If it is not registered, stop and ask before adding
   it. An unregistered source type causes silent foreign-key failures where the
   ingest appears to succeed but the count stays at zero.

2. **Normalize identity.** Deduplicate on LinkedIn URL first, then email, then
   normalized name plus company domain. Prefer UPSERT over INSERT so a re-run
   accumulates rather than duplicates.

3. **Cache before you parse.** If this ingest calls a paid API, write the raw
   response to `enrichment_calls` before deriving any rows from it. Check that
   cache before making the call. A parsing bug discovered later must never force
   re-paying for the data.

4. **Flush per batch.** Commit every 25 records. Do not accumulate thousands of
   rows in memory and write once at the end. A network blip hours in must not
   erase the work.

5. **Report.** Print net-new leads, updated leads, skipped duplicates, and the
   cost of any paid calls made versus served from cache.

## Verify

```sql
SELECT source_type, source_label, count(*)
FROM lead_sources GROUP BY 1, 2 ORDER BY 3 DESC;
```

## Instances

This command accepts `--instance <name>` to select which MarketBase database to act on. See `/marketbase:instances`. When more than one instance is configured and the user has not said which, ask rather than guessing.
