-- Named region groups for territory presentation.
--
-- 051 made the territory itself a client fact (`regions`, a flat ISO2 set read
-- by the rep engine's KEEP/CUT rules). What it cannot say is how those
-- countries GROUP when a human reads a report: "North America / Europe / Rest
-- of world" are columns, and a client with a custom market definition gets
-- their own columns, shown exactly as defined (decided 2026-10-06,
-- for competitor-salesforce-report).
--
-- `region_groups` is a jsonb ARRAY of {"name": text, "countries": [ISO2,...]}
-- objects -- an array, not an object, because jsonb objects do not preserve key
-- order and column order is part of the definition. Groups must be disjoint.
--
-- `region_groups` is PRESENTATION, `regions` is the FILTER, and they are
-- deliberately independent (decided 2026-10-06). A group may name countries
-- OUTSIDE `regions`: those columns are informational -- they show where a
-- competitor's sales force sits even when the client does not cover it, so
-- quota-carrying reps there tally but are never addable (the engine keeps
-- reading the flat `regions` set). The first live example is a US-only
-- coverage territory analysed with United States / Canada / UK and Ireland /
-- Europe / Other tier one columns. Groups must still be disjoint. NULL
-- region_groups with a non-empty `regions` is legal (a 051-era row) and
-- renders as one unnamed in-territory group; new writers always fill both.
--
-- In a client's own marketbase, `scope` stays as 051 defined it ('default' =
-- client-wide). In Impact 11's OWN marketbase -- which sizes prospects before
-- they have a database -- `scope` carries the prospect's name instead, one row
-- per prospect. The two uses never meet in one database.

ALTER TABLE territory_policy ADD COLUMN IF NOT EXISTS region_groups jsonb;

COMMENT ON COLUMN territory_policy.region_groups IS
    'Ordered jsonb array of {"name","countries"} groups = report columns. '
    'Presentation only, independent of `regions` (the coverage filter): a group '
    'outside `regions` is informational. NULL = legacy row, one unnamed group.';
