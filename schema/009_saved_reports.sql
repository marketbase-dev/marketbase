-- MarketBase schema, migration 009 — Saved reports + run history
--
-- Lets us register named SQL queries (or Postgres views) with metadata —
-- description, purpose, when it was created and why — and log each time the
-- report is run.
--
-- Archiving = set archived_at; the underlying view stays callable. Reports
-- never get hard-deleted (so historical run logs always have a parent).

CREATE TABLE IF NOT EXISTS saved_reports (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text UNIQUE NOT NULL,                -- e.g. 'thought-leader-engager-report'
    description     text,                                 -- one-line summary
    purpose         text,                                 -- why we created this — the analyst note
    sql_query       text NOT NULL,                        -- the actual SQL (with $1/$2 bind vars if parameterized)
    view_name       text,                                 -- optional: name of the Postgres VIEW that wraps the query
    output_columns  jsonb,                                -- optional: per-column display metadata for exports
    created_by      text,                                 -- 'alice' / 'claude' / script name
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_run_at     timestamptz,
    archived_at     timestamptz,                          -- when set, the report is marked old; view itself still works
    archive_reason  text
);

CREATE INDEX IF NOT EXISTS idx_saved_reports_archived ON saved_reports(archived_at);

CREATE TABLE IF NOT EXISTS saved_report_runs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id    uuid NOT NULL REFERENCES saved_reports(id) ON DELETE CASCADE,
    params       jsonb,                                   -- bind variables for this run
    row_count    integer,                                 -- N rows returned
    output_path  text,                                    -- if exported to a file
    ran_at       timestamptz NOT NULL DEFAULT now(),
    ran_by       text,                                    -- 'alice' / 'claude' / cron
    notes        text                                     -- analyst note about this specific run
);

CREATE INDEX IF NOT EXISTS idx_saved_report_runs_report ON saved_report_runs(report_id, ran_at DESC);
