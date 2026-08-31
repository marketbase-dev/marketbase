-- New lead_sources.source_type value for CSVs sourced by searching for
-- B2B marketing consultants who recently posted (e.g. Acme-AI's
-- carousel-1 consultants list). Conceptually parallel to other
-- search-style source types like 'find_senior_execs' and 'apollo_export'.
--
-- Postgres requires ALTER TYPE ... ADD VALUE to run outside a transaction
-- block, AND it cannot be inside a DO block. So this migration is a
-- single ALTER statement; IF NOT EXISTS makes it idempotent across re-runs.

ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'marketing_consultants_search';
