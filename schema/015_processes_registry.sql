-- MarketBase schema, migration 015 — Processes registry
--
-- A generalized registry of every named, versioned process that writes to or
-- reads from the MarketBase: qualifiers, researchers, reporters, pipelines.
--
-- Each (name, version) row carries the canonical YAML spec inline. Versions
-- are immutable: change the spec → write a new row with a bumped version.
-- Old qualifications / runs reference their original version, so historical
-- decisions remain fully explainable.
--
-- Soft-linked from existing tables:
--   lead_qualifications.qualifier_name + qualifier_version → processes.name + processes.version (where process_type='qualifier')
--   saved_reports.depends_on_processes (added in 016) → array of 'name@version' strings
--   searches.skill — could be matched to processes.name where process_type='researcher'
--
-- No FK constraint — soft-link keeps writes simple and doesn't break legacy
-- rows that pre-date the registry.

CREATE TABLE IF NOT EXISTS processes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Identity
    name            text NOT NULL,                    -- e.g. 'deep-network-check', 'engagers-research', 'thought-leader-engager-report'
    version         text NOT NULL,                    -- e.g. '2026-05-acme-ai', '1.0', '2026-06-acme-ai-v2'
    process_type    text NOT NULL,                    -- 'qualifier' | 'researcher' | 'reporter' | 'pipeline' | 'enricher'

    -- Documenacmen
    description     text,                             -- one-line summary (extracted from yaml_spec)
    yaml_spec       text NOT NULL,                    -- the full YAML — canonical source of truth
    inputs          jsonb,                            -- extracted for queryability: {fields_consulted, depends_on_processes, …}
    outputs         jsonb,                            -- extracted for queryability: {writes_to_tables, persona_values, …}
    rule_changes    text,                             -- changelog vs prior version

    -- Versioning / audit
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text,
    superseded_by   uuid REFERENCES processes(id) ON DELETE SET NULL,

    CONSTRAINT uq_processes_name_version UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS idx_processes_name          ON processes(name);
CREATE INDEX IF NOT EXISTS idx_processes_process_type  ON processes(process_type);
CREATE INDEX IF NOT EXISTS idx_processes_superseded_by ON processes(superseded_by);

COMMENT ON TABLE processes IS
'Registry of every named, versioned process that operates on the MarketBase. Each row is the canonical YAML spec for one (process_name, version). Soft-linked from lead_qualifications (qualifier_name + qualifier_version) and saved_reports (depends_on_processes array).';
