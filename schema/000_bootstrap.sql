-- 000_bootstrap.sql
-- Creates the migration tracking table. Safe to re-run.

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE schema_migrations IS
    'One row per applied migration file. Written by the migration runner.';
