-- Where a client's selling territory lives.
--
-- It used to live in two places, both bad. For most clients it was the global
-- TIER_ONE set inside the people-search engine -- one policy for every customer,
-- edited by whoever needed it last. For one client it lived in a call transcript
-- and nowhere else, which cost real results: the version we would have applied
-- was two months old and said "US only", while a call two weeks before the run
-- had put Europe in scope after that client signed a European SDR vendor. The
-- stale rule would have discarded 139 UK and 31 Europe candidates and 6 of 47
-- recent joiners -- right after they bought the capacity to work them. It was
-- only caught because two recent transcripts were missing from the client folder
-- and got synced while checking something else.
--
-- A territory is a client fact that outlives any single run, so it belongs in the
-- client's own database with its evidence attached. `source` cites the artifact
-- the decision came from; `rationale` says why. Both are NOT NULL on purpose: a
-- territory with no provenance is exactly how that situation recurs.
--
-- `scope` is keyed rather than a bare single row so a per-competitor override can
-- be added later without a schema change; 'default' is the client-wide policy.
--
-- `regions` holds ISO 3166-1 alpha-2 codes. An empty array means "no territory
-- filter" -- deliberately NOT the same as having no row, which means nobody has
-- decided yet. Code must treat those two differently: no row is a question, an
-- empty array is an answer. In particular, no row must never silently fall back
-- to the old global set, because that is the behaviour this table replaces.

CREATE TABLE IF NOT EXISTS territory_policy (
    scope       text        PRIMARY KEY DEFAULT 'default',
    regions     text[]      NOT NULL,
    source      text        NOT NULL,
    rationale   text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE  territory_policy IS
    'Per-client selling territory as ISO2 codes, with the evidence it came from. '
    'Read by the competitor-rep engine in place of a global tier-one set.';
COMMENT ON COLUMN territory_policy.regions IS
    'ISO 3166-1 alpha-2. Empty array = no territory filter. No row = undecided.';
COMMENT ON COLUMN territory_policy.source IS
    'The artifact this came from, citable: a call transcript id, a doc, a person and a date.';
COMMENT ON COLUMN territory_policy.rationale IS
    'Why this is the territory. Written for whoever reads it in six months.';
