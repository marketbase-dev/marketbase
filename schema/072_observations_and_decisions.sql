-- MarketBase schema, migration 072 — Observations and decisions
--
-- `assertions` (042) holds every claim about an entity, but a row cannot say
-- WHICH kind of claim it is. Two kinds keep getting mixed up:
--
--   observation — what a source showed: "the client's CRM records 34 deals lost
--                 to this company". Chooses nothing. Only as good as its
--                 evidence, so it must cite that evidence: a `signals` row or an
--                 `enrichment_calls` row.
--   decision    — a choice and who made it: "do not monitor this company".
--                 Only as good as its authority, so it must name the decider and
--                 say how it was made (manual | reviewed_batch | automatic, the
--                 vocabulary the ignore reason already uses). It may list the
--                 observations it rests on (`basis`).
--
-- `asserted_by` is NOT the decider. It names the writer, which is usually a
-- tool (backfill_audience_tags, tier_expansion_candidates). Who decided is a
-- separate fact, hence `decided_by`.
--
-- WHY `kind` IS NULLABLE AND NEVER BACKFILLED
--
-- Every instance already holds rows, written by several tools and by the 050
-- tag-removal trigger, and none of them knows about kinds. NULL means
-- "unclassified": written before this migration, or by a writer not yet moved
-- to the kind-aware writer. Classifying old rows after the fact would stamp a
-- claim nobody checked, so they stay NULL. Making `kind` required is a later
-- migration, once every writer sets it.
--
-- The rules below apply only to rows that declare a kind, so existing writers
-- keep working unchanged. An unclassified row may not carry the new columns
-- either: half-classified rows would be the worst of both.
--
-- `subject_meeting_id` adds a meeting as a subject. Bookings-report categories
-- are decisions about a MEETING, never about the person who booked it (one
-- person can book several).

ALTER TABLE assertions ADD COLUMN IF NOT EXISTS kind        text;
ALTER TABLE assertions ADD COLUMN IF NOT EXISTS via         text;
ALTER TABLE assertions ADD COLUMN IF NOT EXISTS decided_by  text;
ALTER TABLE assertions ADD COLUMN IF NOT EXISTS decided_at  timestamptz;
ALTER TABLE assertions ADD COLUMN IF NOT EXISTS basis       uuid[];
ALTER TABLE assertions ADD COLUMN IF NOT EXISTS evidence_enrichment_call_id
    uuid REFERENCES enrichment_calls(id);
ALTER TABLE assertions ADD COLUMN IF NOT EXISTS subject_meeting_id
    uuid REFERENCES lead_meetings(id) ON DELETE CASCADE;

COMMENT ON COLUMN assertions.kind IS
    'observation | decision. NULL = unclassified (written before 072 or by a writer not yet kind-aware). Never backfilled.';
COMMENT ON COLUMN assertions.via IS
    'Decisions only: manual (a person) | reviewed_batch (a person approved a list, a tool applied it) | automatic (a rule decided).';
COMMENT ON COLUMN assertions.decided_by IS
    'Decisions only, required: the accountable person, or for via=automatic the rule and its version. Not the writer; that is asserted_by.';
COMMENT ON COLUMN assertions.decided_at IS
    'Decisions only: when the decision was made, if earlier than asserted_at (when it was recorded).';
COMMENT ON COLUMN assertions.basis IS
    'Decisions only: ids of the observation assertions the decision rests on. Checked on write by trg_assertions_basis.';
COMMENT ON COLUMN assertions.evidence_enrichment_call_id IS
    'Observations: the cached API response the observation was read from. An observation cites this or evidence_signal_id.';

-- ── kind rules ─────────────────────────────────────────────────────────────

DO $$
DECLARE
    rules text[][] := ARRAY[
      ['ck_assertions_kind',
       $c$kind IS NULL OR kind IN ('observation', 'decision')$c$],
      ['ck_assertions_via',
       $c$via IS NULL OR via IN ('manual', 'reviewed_batch', 'automatic')$c$],
      -- a decision names its decider and how it was made
      ['ck_assertions_decision_decider',
       $c$kind IS DISTINCT FROM 'decision'
          OR (decided_by IS NOT NULL AND btrim(decided_by) <> '' AND via IS NOT NULL)$c$],
      -- an automatic decision names the rule VERSION that made it
      ['ck_assertions_decision_automatic_processor',
       $c$kind IS DISTINCT FROM 'decision' OR via IS DISTINCT FROM 'automatic'
          OR processor_id IS NOT NULL$c$],
      -- a decision cites observations through basis, never raw evidence
      ['ck_assertions_decision_no_raw_evidence',
       $c$kind IS DISTINCT FROM 'decision'
          OR (evidence_signal_id IS NULL AND evidence_enrichment_call_id IS NULL)$c$],
      -- an observation cites its evidence
      ['ck_assertions_observation_evidence',
       $c$kind IS DISTINCT FROM 'observation'
          OR num_nonnulls(evidence_signal_id, evidence_enrichment_call_id) >= 1$c$],
      -- an observation chooses nothing
      ['ck_assertions_observation_not_a_decision',
       $c$kind IS DISTINCT FROM 'observation'
          OR num_nonnulls(via, decided_by, decided_at, basis) = 0$c$],
      -- an unclassified row carries none of the new columns
      ['ck_assertions_unclassified_plain',
       $c$kind IS NOT NULL
          OR num_nonnulls(via, decided_by, decided_at, basis, evidence_enrichment_call_id) = 0$c$]
    ];
    i int;
BEGIN
    FOR i IN 1 .. array_length(rules, 1) LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = rules[i][1]) THEN
            EXECUTE format('ALTER TABLE assertions ADD CONSTRAINT %I CHECK (%s)',
                           rules[i][1], rules[i][2]);
        END IF;
    END LOOP;
END$$;

-- basis may only point at existing observations. Arrays cannot carry foreign
-- keys, so a trigger checks it on write.
CREATE OR REPLACE FUNCTION assertions_check_basis() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    wanted int;
    found  int;
BEGIN
    IF NEW.basis IS NULL THEN
        RETURN NEW;
    END IF;
    IF cardinality(NEW.basis) = 0 THEN
        RAISE EXCEPTION 'assertions.basis is empty; use NULL for a decision with no recorded basis'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT count(DISTINCT b) INTO wanted FROM unnest(NEW.basis) b;
    SELECT count(*) INTO found FROM assertions
     WHERE id = ANY (NEW.basis) AND kind = 'observation';
    IF found <> wanted THEN
        RAISE EXCEPTION 'assertions.basis: % of % ids are not existing observations',
                        wanted - found, wanted
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END$$;

DROP TRIGGER IF EXISTS trg_assertions_basis ON assertions;
CREATE TRIGGER trg_assertions_basis
    BEFORE INSERT OR UPDATE OF basis ON assertions
    FOR EACH ROW EXECUTE FUNCTION assertions_check_basis();

-- ── the meeting subject ────────────────────────────────────────────────────

ALTER TABLE assertions DROP CONSTRAINT IF EXISTS assertions_one_subject;
ALTER TABLE assertions ADD CONSTRAINT assertions_one_subject CHECK (num_nonnulls(
    subject_lead_id, subject_company_id, subject_company_edge_id,
    subject_post_engagement_id, subject_conversation_id, subject_meeting_id) = 1);

-- Same index as 044 with the new subject added. NULLS NOT DISTINCT stays
-- load-bearing: every row has five NULL subject columns now.
DROP INDEX IF EXISTS uq_assertions_active;
CREATE UNIQUE INDEX uq_assertions_active ON assertions (
    subject_lead_id, subject_company_id, subject_company_edge_id,
    subject_post_engagement_id, subject_conversation_id, subject_meeting_id,
    namespace, key
) NULLS NOT DISTINCT WHERE removed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_assertions_meeting
    ON assertions (subject_meeting_id) WHERE subject_meeting_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_assertions_kind
    ON assertions (kind, namespace, key) WHERE removed_at IS NULL AND kind IS NOT NULL;

-- ── the registry says which kind each key is ───────────────────────────────

ALTER TABLE tag_definitions ADD COLUMN IF NOT EXISTS kind text;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tag_definitions_kind') THEN
        ALTER TABLE tag_definitions ADD CONSTRAINT ck_tag_definitions_kind
            CHECK (kind IS NULL OR kind IN ('observation', 'decision'));
    END IF;
END$$;
COMMENT ON COLUMN tag_definitions.kind IS
    'observation | decision: the kind every assertion under this key must declare. NULL = not yet classified.';

INSERT INTO tag_definitions (namespace, key, entity_type, layer, kind, description) VALUES
  ('client_evidence', 'deals_lost', 'company', 'attribute', 'observation',
   'Deals the client lost to this company, as the client''s CRM records them. value = the count; '
   'notes = the field or query read and its as-of date. Evidence: the cached CRM response.'),
  ('client_evidence', 'selection_sheet_priority', 'company', 'attribute', 'observation',
   'The priority the client gave this company in the target-selection sheet they reviewed '
   '(e.g. P1, P2, DQ). value = the priority as written; notes = the sheet and tab. Evidence: the sheet read.'),
  ('client_evidence', 'engagement_roster', 'company', 'attribute', 'observation',
   'The client''s own social-engagement roster lists people at this company. value = how many; '
   'notes = the roster and the role it gives them. Evidence: the roster read.'),
  ('client_evidence', 'g2_signals', 'company', 'attribute', 'observation',
   'Buyer-intent rows in the client''s G2 feed that name this company as a compared vendor. '
   'value = the count; notes = the window. Evidence: the feed read.'),
  ('monitoring', 'monitor', 'company', 'state', 'decision',
   'Whether we monitor this company''s sellers for the client. value = monitor | do_not_monitor; '
   'notes = the reason, which reports print. One active decision per company; a change supersedes it.')
ON CONFLICT (namespace, key, entity_type) DO NOTHING;

-- ── read paths ─────────────────────────────────────────────────────────────
-- Active rows only, like active_assertions. History (superseded decisions,
-- replaced observations) stays readable in `assertions`.

CREATE OR REPLACE VIEW v_observations AS
SELECT a.id,
       CASE WHEN a.subject_lead_id            IS NOT NULL THEN 'lead'
            WHEN a.subject_company_id         IS NOT NULL THEN 'company'
            WHEN a.subject_company_edge_id    IS NOT NULL THEN 'company_edge'
            WHEN a.subject_post_engagement_id IS NOT NULL THEN 'post_engagement'
            WHEN a.subject_conversation_id    IS NOT NULL THEN 'conversation'
            WHEN a.subject_meeting_id         IS NOT NULL THEN 'meeting' END AS subject_type,
       coalesce(a.subject_lead_id, a.subject_company_id, a.subject_company_edge_id,
                a.subject_post_engagement_id, a.subject_conversation_id,
                a.subject_meeting_id) AS subject_id,
       a.namespace, a.key, a.value, a.notes, a.confidence,
       a.evidence_signal_id, a.evidence_enrichment_call_id,
       coalesce(s.enriched_at, ec.fetched_at) AS evidence_at,
       coalesce(s.enricher_name, ec.api || ' ' || ec.endpoint) AS evidence_source,
       a.processor_id, a.asserted_by, a.asserted_at
FROM assertions a
LEFT JOIN signals s           ON s.id  = a.evidence_signal_id
LEFT JOIN enrichment_calls ec ON ec.id = a.evidence_enrichment_call_id
WHERE a.kind = 'observation' AND a.removed_at IS NULL;

CREATE OR REPLACE VIEW v_decisions AS
SELECT a.id,
       CASE WHEN a.subject_lead_id            IS NOT NULL THEN 'lead'
            WHEN a.subject_company_id         IS NOT NULL THEN 'company'
            WHEN a.subject_company_edge_id    IS NOT NULL THEN 'company_edge'
            WHEN a.subject_post_engagement_id IS NOT NULL THEN 'post_engagement'
            WHEN a.subject_conversation_id    IS NOT NULL THEN 'conversation'
            WHEN a.subject_meeting_id         IS NOT NULL THEN 'meeting' END AS subject_type,
       coalesce(a.subject_lead_id, a.subject_company_id, a.subject_company_edge_id,
                a.subject_post_engagement_id, a.subject_conversation_id,
                a.subject_meeting_id) AS subject_id,
       a.namespace, a.key, a.value,
       a.notes AS reason,
       a.decided_by, a.via,
       coalesce(a.decided_at, a.asserted_at) AS decided_at,
       a.basis, a.processor_id, a.confidence,
       a.asserted_by, a.asserted_at
FROM assertions a
WHERE a.kind = 'decision' AND a.removed_at IS NULL;

COMMENT ON VIEW v_observations IS 'Active observations with their evidence. Read this, not assertions.';
COMMENT ON VIEW v_decisions    IS 'Active decisions with decider, via and basis. Read this, not assertions.';

-- 059's view, unchanged, plus the active monitoring decision appended
-- (CREATE OR REPLACE can only append columns).
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
       END AS conflict,
       md.value      AS monitoring_decision,
       md.reason     AS monitoring_reason,
       md.decided_by AS monitoring_decided_by,
       md.decided_at AS monitoring_decided_at
FROM companies c
JOIN company_relationships cr ON cr.company_id = c.id
LEFT JOIN v_decisions md ON md.subject_id = c.id AND md.subject_type = 'company'
                        AND md.namespace = 'monitoring' AND md.key = 'monitor'
WHERE cr.relationship = 'competitor'
ORDER BY c.name;
