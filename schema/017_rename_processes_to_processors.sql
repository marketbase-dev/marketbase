-- MarketBase schema, migration 017 — Rename "process" → "processor" everywhere
--
-- "process" was too generic and collided with OS / business-process meanings.
-- "processor" is more precise: a named, versioned definition of an operation
-- that reads/writes the MarketBase. Same shape, clearer vocabulary.
--
-- Also rename type values to be more accurate:
--   qualifier  → classifier  (precise: assigns a label/persona)
--   researcher → fetcher     (precise: fetches external data)
--   pipeline   → orchestrator (avoids GTM-collision with "sales pipeline")
--   reporter   → reporter    (unchanged)
--   enricher   → enricher    (unchanged)

ALTER TABLE processes RENAME TO processors;
ALTER TABLE processors RENAME COLUMN process_type TO processor_type;

ALTER TABLE saved_reports RENAME COLUMN depends_on_processes TO depends_on_processors;

UPDATE processors SET processor_type = 'classifier'   WHERE processor_type = 'qualifier';
UPDATE processors SET processor_type = 'fetcher'      WHERE processor_type = 'researcher';
UPDATE processors SET processor_type = 'orchestrator' WHERE processor_type = 'pipeline';

-- Indexes referencing the old column name auto-rename when the column does.
-- The UNIQUE constraint and FK self-reference auto-follow the table rename.

COMMENT ON TABLE processors IS
'Registry of every named, versioned processor (classifier, fetcher, orchestrator, enricher, reporter) that operates on the MarketBase. Renamed from "processes" in migration 017.';
