-- MarketBase schema, migration 016 — Saved-report documenacmen fields
--
-- Extends `saved_reports` with structured documenacmen so every report
-- explains itself:
--
--   column_definitions   — per-column metadata (description + which process produced the value)
--   depends_on_processes — array of 'process_name@version' strings (soft-link to processes table)
--   assumptions          — analyst caveats / known issues
--
-- All nullable so existing reports continue to work; new reports populate
-- these as a best practice. The `marketbase-explain-report` skill renders all
-- three plus pulls the dependent processes' YAML specs.

ALTER TABLE saved_reports ADD COLUMN IF NOT EXISTS column_definitions    jsonb;
ALTER TABLE saved_reports ADD COLUMN IF NOT EXISTS depends_on_processes  text[];
ALTER TABLE saved_reports ADD COLUMN IF NOT EXISTS assumptions           text;

COMMENT ON COLUMN saved_reports.column_definitions IS
'Array of {name, description, source} objects describing each output column. `source` is a free-form pointer like "process:deep-network-check@2026-05-acme-ai/persona" or "computed: COUNT(*) of engagers with persona=...".';
COMMENT ON COLUMN saved_reports.depends_on_processes IS
'Soft-link to processes.name + processes.version, joined with @. Example: ARRAY[''deep-network-check@2026-05-acme-ai'', ''basic-creator-check@2026-05-acme-ai'']. Used by marketbase-explain-report to surface the dependent YAML specs.';
COMMENT ON COLUMN saved_reports.assumptions IS
'Free-text caveats. Example: "Auroriele Hans''s engager count may be undercounted until the slug-mismatch bug is fixed — see Acme-AI/GTM-TODO.md."';
