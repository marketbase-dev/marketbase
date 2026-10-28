-- Per-member campaign parameters.
--
-- A campaign's message is often a template whose blanks differ per recipient:
-- which products to name, which incentive to offer, which opener to use. Until
-- now campaign_members had only a free-text `notes` column, so those values had
-- nowhere structured to live and the sequencer export had to recompute them.
--
-- `params` holds them as a flat JSON object of merge fields, e.g.
--   {"comparable_set": "Acme, Northwind, Globex", "compensation_rate": 200}
-- Keys are the campaign's own vocabulary; nothing here constrains them beyond
-- "an object", because each campaign defines its own blanks. Values that are
-- the same for every member belong on the campaign row, not here.
--
-- Written by stage_to_campaign.py (--params-from-csv). Re-staging merges new
-- keys over old ones (params || new), so a re-run can add a field without
-- erasing the others.

ALTER TABLE campaign_members
    ADD COLUMN IF NOT EXISTS params jsonb NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'campaign_members_params_is_object') THEN
        ALTER TABLE campaign_members
            ADD CONSTRAINT campaign_members_params_is_object
            CHECK (jsonb_typeof(params) = 'object');
    END IF;
END $$;
