-- MarketBase schema, migration 059 — What the CLIENT said about a company
--
-- Adds `client_disposition` to company_relationships. Another axis orthogonal to
-- the ones already modelled here (see 035's header for the full list):
--   • relationship / scope / status  — what the company is, and whether workable
--   • monitor_desired                — whether WE chose to monitor it
--   • client_disposition             — whether THE CLIENT chose it  ← new
--
-- WHY THIS IS NOT monitor_desired
--
-- `monitor_desired` is declared `NOT NULL DEFAULT false`, and 035 defines it as
-- "we have DELIBERATELY chosen to monitor this company". Both halves of that
-- matter and together they make the column unable to answer a client question:
--
--   * it records OUR intent, not the client's approval; and
--   * because it defaults to false and cannot be null, a company nobody ever
--     asked about is stored identically to one the client actively rejected.
--
-- So `monitor_desired = false` has two unrelated meanings in the same column,
-- and nothing distinguishes them. Observed 2026-10-17 on one instance: of 60
-- false rows, 38 were real client rejections recorded from a reviewed workbook
-- and 22 were companies that had simply never been put in front of the client
-- (they were landscape rows carrying the column default, with zero reps held).
-- Reading the boolean alone produced a confident wrong answer about what the
-- client had agreed to — twice in one session, in both directions.
--
-- The three states this column exists to keep apart:
--
--   'selected'     — the client reviewed it and said yes. Monitoring it is
--                    something they asked for; not monitoring it is a gap we owe
--                    them.
--   'deselected'   — the client reviewed it and said no. Monitoring it anyway is
--                    a breach of their instruction. This is the only state that
--                    forbids anything.
--   'not_offered'  — the client has never seen it. Carries NO client signal in
--                    either direction, and must never be read as a rejection.
--                    An unoffered company is a candidate, not a refusal.
--   NULL           — not established yet. Distinct from 'not_offered': NULL
--                    means we have not checked what the client was shown,
--                    'not_offered' means we checked and they were shown nothing.
--
-- `client_disposition_source` must name the ARTIFACT that decided it — the
-- reviewed workbook, call or message — not a person and not a tool. A
-- disposition whose source is unciteable is indistinguishable from a guess, and
-- a guess in this column is worse than a null: it is the column downstream
-- readers will trust to mean "the client said so".
--
-- Deliberately nullable and with no default, unlike `monitor_desired`. A default
-- here would recreate the exact defect this migration exists to fix.

ALTER TABLE company_relationships
    ADD COLUMN IF NOT EXISTS client_disposition        text;
ALTER TABLE company_relationships
    ADD COLUMN IF NOT EXISTS client_disposition_at     date;
ALTER TABLE company_relationships
    ADD COLUMN IF NOT EXISTS client_disposition_source text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_company_relationship_client_disposition'
    ) THEN
        ALTER TABLE company_relationships
            ADD CONSTRAINT ck_company_relationship_client_disposition
            CHECK (client_disposition IS NULL
                   OR client_disposition IN ('selected', 'deselected', 'not_offered'));
    END IF;
END$$;

-- A disposition is a claim about something the client was shown, so it has to be
-- citeable. 'not_offered' is exempt on purpose: its evidence is the ABSENCE of
-- the company from the reviewed artifact, which no single source string names.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_company_relationship_client_disposition_cited'
    ) THEN
        ALTER TABLE company_relationships
            ADD CONSTRAINT ck_company_relationship_client_disposition_cited
            CHECK (client_disposition IS DISTINCT FROM 'selected'
                     AND client_disposition IS DISTINCT FROM 'deselected'
                   OR client_disposition_source IS NOT NULL);
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_company_relationships_client_disposition
    ON company_relationships(client_disposition)
 WHERE client_disposition IS NOT NULL;

-- Re-expose on the canonical competitor view. CREATE OR REPLACE can only append
-- columns, so the three new fields go at the end, after the monitoring-intent
-- pair added by 035.
CREATE OR REPLACE VIEW v_competitor_companies AS
SELECT c.id, c.linkedin_slug, c.name, c.linkedin_url, c.website, c.industry,
       c.employee_count, c.employee_range,
       cr.scope, cr.source AS tag_source, cr.notes, cr.recorded_at,
       c.is_showcase, c.parent_linkedin_slug,
       cr.status,
       cr.monitor_desired, cr.monitor_desired_at,
       cr.client_disposition, cr.client_disposition_at, cr.client_disposition_source
FROM companies c
JOIN company_relationships cr ON cr.company_id = c.id
WHERE cr.relationship = 'competitor'
ORDER BY c.name;

-- The two gaps worth watching, named here so a reader does not have to rederive
-- them. Both are only answerable once `client_disposition` is populated:
--   * selected but nobody monitored  — we owe the client coverage they asked for
--   * deselected but still monitored — we are monitoring against instruction
-- The second is the serious one; the first was real on one instance at the time
-- this migration was written (four selected companies with reps held and no
-- person enrolled), and had been invisible because nothing compared the two
-- axes.
CREATE OR REPLACE VIEW v_company_monitoring_vs_client AS
SELECT c.id AS company_id,
       c.name,
       cr.client_disposition,
       cr.monitor_desired,
       (SELECT count(DISTINCT ls.lead_id)
          FROM lead_sources ls
         WHERE ls.source_type = 'find_competitor_salesperson'
           AND ls.source_label LIKE c.name || '%') AS reps_held,
       CASE
         WHEN cr.client_disposition = 'deselected' AND cr.monitor_desired
           THEN 'monitoring_against_client_instruction'
         WHEN cr.client_disposition = 'selected' AND NOT cr.monitor_desired
           THEN 'client_asked_but_we_opted_out'
         ELSE NULL
       END AS conflict
FROM companies c
JOIN company_relationships cr ON cr.company_id = c.id
WHERE cr.relationship = 'competitor'
ORDER BY c.name;
