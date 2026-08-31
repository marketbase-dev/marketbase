-- MarketBase schema, migration 037 — index the enrichment_calls cache lookup
--
-- The read-through cache convention (never re-pay a metered API) looks calls up by
-- (api, endpoint, params). Without an index that is a sequential scan per lookup,
-- which gets slower as the table grows — exactly backwards, since the cache earns
-- its keep on the runs with the most calls. jsonb has a btree opclass, so the
-- params column can sit in the index directly.

CREATE INDEX IF NOT EXISTS idx_enrichment_cache_lookup
    ON enrichment_calls (api, endpoint, params)
    WHERE success;
