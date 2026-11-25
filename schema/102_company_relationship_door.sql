-- 102_company_relationship_door.sql
-- One write door for what a company is to a client, in the company-link format.
--
-- WHY
--
-- `company_relationships` (007, 033, 035, 056, 059) holds one row per company
-- and relationship type. Two defects make it the wrong format to keep:
--
--   * it cannot name the counterparty. "Competitor" always means "competitor of
--     whoever owns this database". That holds while one database serves one
--     client and stops holding the moment two databases that serve several
--     clients merge, which is the plan for this one and the connection-tracking
--     app's.
--   * it has no history. A change overwrites the row; the old state survives
--     only as prose someone remembered to paste into `notes`.
--
-- ASSERTIONS_MIGRATION_PLAN.md (3.4, 3.5) already chose the replacement and it
-- was carried out for customers (company_edges + assertions) but never for
-- this table: a company link (`company_edges`, identity only, never deleted)
-- that names both companies, and every judgment about it as an assertion whose
-- subject is the link, closed (removed_at) rather than overwritten.
--
-- THE FORMAT (every fact separate; an earlier design mixed "what it is" with
-- "do we monitor it" in one field and was withdrawn for exactly that reason)
--
--   relationship/<type>       the link's live marker. A link is live while its
--                             marker is active. competitor + confidence
--                             'suspected' = a possible competitor (the old
--                             'suspected_competitor' row), so reclassifying
--                             keeps the link and its decisions.
--   relationship/scope        direct | adjacent | aspirational
--   relationship/status       clear | conflicting | checking  (conflict vetting)
--   monitoring/monitor        monitor | do_not_monitor   (decision)
--   client/choice             selected | deselected | not_offered (decision; the
--                             client decided, the notes cite the artifact)
--   scan/leave_out            leave_out  (decision: leave this company out of
--                             the client's scans)
--   discovery/rep_discovery   page_sweep | acquired_brand; discovery/config is
--                             its JSON (056's two columns)
--   client/own_company        on a COMPANY: primary | brand. The instance's own
--                             company; links to the primary one mirror the old
--                             table.
--
-- THE DOOR
--
--   set_company_relationship()  create or change a link (type, possible, scope)
--   set_link_decision()         monitor, client choice, leave out, vetting
--   stop_monitoring()           do_not_monitor AND keep leaving it out
--   withdraw_company_link()     added by mistake: closes everything live on the
--                               link, deletes nothing
--
-- An identical write is a no-op; a changed one closes the active claim with
-- "superseded by <id>" and opens the new one (lib/assertions.py's rule, here in
-- SQL so a TypeScript caller gets the same door).
--
-- TRANSITION
--
-- Until the old table becomes a read-only view (ticket 04), the door also keeps
-- its row current for links to the primary own company, field by field: only
-- the fact a call wrote is copied, so a door write before the conversion
-- (ticket 02) never blanks a value the old row holds. While that mirroring is
-- on, withdrawing a mirrored link is refused: the old table has no way to say
-- "withdrawn" short of a DELETE, and nothing here deletes.
--
-- `v_company_relationships_from_links` shows the new format in the old table's
-- shape. The conversion and the swap compare it with the table, column by
-- column.

-- ── registry ───────────────────────────────────────────────────────────────
INSERT INTO tag_definitions (namespace, key, entity_type, layer, description, kind) VALUES
  ('relationship', 'competitor', 'company_edge', 'relationship',
   'The from-company sells against the to-company. The link''s live marker: the link is live while it is active. '
   'confidence=suspected marks a possible competitor. How close is relationship/scope.', NULL),
  ('relationship', 'other', 'company_edge', 'relationship',
   'Related to the to-company in another way (a parent, an acquirer, an adjacent company); the note says how. Live marker.', NULL),
  ('relationship', 'scope', 'company_edge', 'relationship',
   'How close a competitor is: direct | adjacent | aspirational.', NULL),
  ('relationship', 'status', 'company_edge', 'state',
   'Conflict-of-interest vetting: clear | conflicting | checking.', 'decision'),
  ('monitoring', 'monitor', 'company_edge', 'state',
   'Whether the from-company''s sellers are monitored for the to-company (the client): monitor | do_not_monitor. '
   'notes = the reason, which reports print.', 'decision'),
  ('client', 'choice', 'company_edge', 'state',
   'What the client said about the from-company: selected | deselected | not_offered. decided_by names the client; '
   'notes cite the artifact (reviewed workbook, call, message). not_offered is never a rejection.', 'decision'),
  ('scan', 'leave_out', 'company_edge', 'state',
   'Leave the from-company out of the to-company''s scans: value leave_out. Stopping monitoring keeps it; only '
   'withdrawing the link as added by mistake takes it off.', 'decision'),
  ('discovery', 'rep_discovery', 'company_edge', 'attribute',
   'How the from-company''s sellers are found: page_sweep | acquired_brand (056).', NULL),
  ('discovery', 'config', 'company_edge', 'attribute',
   'The acquired-brand method''s parameters as JSON text (056 discovery_config).', NULL),
  ('client', 'own_company', 'company', 'state',
   'This company is the instance''s own: primary (links point at it) | brand (another own brand).', NULL)
ON CONFLICT (namespace, key, entity_type) DO UPDATE
   SET description = EXCLUDED.description, layer = EXCLUDED.layer, kind = EXCLUDED.kind;

-- ── the instance's own company ─────────────────────────────────────────────
CREATE OR REPLACE FUNCTION own_company() RETURNS uuid
LANGUAGE plpgsql STABLE AS $$
DECLARE
    ids uuid[];
BEGIN
    SELECT array_agg(subject_company_id) INTO ids FROM assertions
     WHERE namespace = 'client' AND key = 'own_company' AND value = 'primary'
       AND subject_company_id IS NOT NULL AND removed_at IS NULL;
    IF ids IS NULL THEN
        RETURN NULL;
    END IF;
    IF cardinality(ids) > 1 THEN
        RAISE EXCEPTION 'own_company(): % companies are marked primary; exactly one may be', cardinality(ids)
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN ids[1];
END$$;

-- ── links ──────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION company_link(_from uuid, _to uuid, _relationship text) RETURNS uuid
LANGUAGE plpgsql AS $$
DECLARE
    e uuid;
BEGIN
    IF _from IS NULL OR _to IS NULL OR _relationship IS NULL THEN
        RAISE EXCEPTION 'company_link: from, to and relationship are required' USING ERRCODE = 'check_violation';
    END IF;
    IF _from = _to THEN
        RAISE EXCEPTION 'company_link: a company cannot be linked to itself' USING ERRCODE = 'check_violation';
    END IF;
    PERFORM 1 FROM tag_definitions
     WHERE namespace = 'relationship' AND key = _relationship AND entity_type = 'company_edge';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'company_link: relationship % is not registered in tag_definitions for company_edge', _relationship
            USING ERRCODE = 'check_violation';
    END IF;
    IF _relationship IN ('scope', 'status') THEN
        RAISE EXCEPTION 'company_link: % is a fact about a link, not a kind of link', _relationship
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT id INTO e FROM company_edges
     WHERE from_company_id = _from AND to_company_id = _to AND relationship = _relationship;
    IF e IS NULL THEN
        INSERT INTO company_edges (from_company_id, to_company_id, relationship)
        VALUES (_from, _to, _relationship)
        ON CONFLICT (from_company_id, to_company_id, relationship) DO NOTHING
        RETURNING id INTO e;
        IF e IS NULL THEN
            SELECT id INTO e FROM company_edges
             WHERE from_company_id = _from AND to_company_id = _to AND relationship = _relationship;
        END IF;
    END IF;
    RETURN e;
END$$;

-- One claim on one link: insert, supersede, or leave unchanged. The core every
-- door function goes through.
CREATE OR REPLACE FUNCTION link_assert(
    _edge uuid, _namespace text, _key text, _value text, _asserted_by text, _notes text,
    _decided_by text DEFAULT NULL, _via text DEFAULT NULL, _decided_at timestamptz DEFAULT NULL,
    _confidence text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    reg_kind text;
    reg_found boolean := false;
    new_kind text;
    old_id uuid;
    o_value text; o_notes text; o_decided_by text; o_via text; o_decided_at timestamptz;
    o_confidence text; o_kind text;
    new_id uuid;
BEGIN
    IF _asserted_by IS NULL OR btrim(_asserted_by) = '' THEN
        RAISE EXCEPTION 'link_assert: asserted_by (the writer) is required' USING ERRCODE = 'check_violation';
    END IF;
    SELECT true, kind INTO reg_found, reg_kind FROM tag_definitions
     WHERE namespace = _namespace AND key = _key AND entity_type = 'company_edge';
    IF NOT coalesce(reg_found, false) THEN
        RAISE EXCEPTION 'link_assert: %/% is not registered for company_edge', _namespace, _key
            USING ERRCODE = 'check_violation';
    END IF;
    IF reg_kind = 'observation' THEN
        RAISE EXCEPTION 'link_assert: %/% is an observation; write it with lib.assertions.observe (it needs evidence)',
            _namespace, _key USING ERRCODE = 'check_violation';
    END IF;
    new_kind := CASE WHEN reg_kind = 'decision' OR _decided_by IS NOT NULL THEN 'decision' END;
    IF new_kind = 'decision' THEN
        IF _decided_by IS NULL OR btrim(_decided_by) = '' THEN
            RAISE EXCEPTION 'link_assert: %/% is a decision and needs decided_by', _namespace, _key
                USING ERRCODE = 'check_violation';
        END IF;
        IF _via IS NULL OR _via NOT IN ('manual', 'reviewed_batch') THEN
            RAISE EXCEPTION 'link_assert: a decision through this door needs via manual or reviewed_batch (got %)', _via
                USING ERRCODE = 'check_violation';
        END IF;
        IF _notes IS NULL OR btrim(_notes) = '' THEN
            RAISE EXCEPTION 'link_assert: a decision needs a reason in notes; reports print it'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF _via IS NOT NULL OR _decided_at IS NOT NULL THEN
        RAISE EXCEPTION 'link_assert: via and decided_at belong to a decision; pass decided_by too'
            USING ERRCODE = 'check_violation';
    END IF;
    PERFORM 1 FROM company_edges WHERE id = _edge;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'link_assert: no company link %', _edge USING ERRCODE = 'check_violation';
    END IF;

    SELECT id, value, notes, decided_by, via, decided_at, confidence, kind
      INTO old_id, o_value, o_notes, o_decided_by, o_via, o_decided_at, o_confidence, o_kind
      FROM assertions
     WHERE subject_company_edge_id = _edge AND namespace = _namespace AND key = _key AND removed_at IS NULL;
    IF old_id IS NOT NULL THEN
        IF o_value IS NOT DISTINCT FROM _value AND o_notes IS NOT DISTINCT FROM _notes
           AND o_decided_by IS NOT DISTINCT FROM _decided_by AND o_via IS NOT DISTINCT FROM _via
           AND o_decided_at IS NOT DISTINCT FROM _decided_at AND o_confidence IS NOT DISTINCT FROM _confidence
           AND o_kind IS NOT DISTINCT FROM new_kind THEN
            RETURN jsonb_build_object('id', old_id, 'outcome', 'unchanged');
        END IF;
        UPDATE assertions SET removed_at = now(), removed_by = _asserted_by, removed_reason = 'superseded'
         WHERE id = old_id AND removed_at IS NULL;
    END IF;

    INSERT INTO assertions (subject_company_edge_id, namespace, key, value, confidence, notes,
                            asserted_by, kind, decided_by, via, decided_at)
    VALUES (_edge, _namespace, _key, _value, _confidence, _notes,
            _asserted_by, new_kind, _decided_by, _via, _decided_at)
    RETURNING id INTO new_id;

    IF old_id IS NOT NULL THEN
        UPDATE assertions SET removed_reason = 'superseded by ' || new_id WHERE id = old_id;
        RETURN jsonb_build_object('id', new_id, 'outcome', 'superseded', 'superseded_id', old_id);
    END IF;
    RETURN jsonb_build_object('id', new_id, 'outcome', 'inserted');
END$$;

CREATE OR REPLACE FUNCTION link_is_live(_edge uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM assertions a JOIN company_edges e ON e.id = a.subject_company_edge_id
                    WHERE e.id = _edge AND a.namespace = 'relationship' AND a.key = e.relationship
                      AND a.removed_at IS NULL)
$$;

-- ── transition: keep the old row current ───────────────────────────────────
-- Copies ONE fact of a link to the primary own company into the old table.
-- A no-op once company_relationships is no longer a plain table, and for links
-- to any other company.
CREATE OR REPLACE FUNCTION company_relationship_legacy_sync(_edge uuid, _fact text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    e company_edges%ROWTYPE;
    own uuid;
    m_id uuid; m_confidence text; m_decided_by text; m_asserted_by text; m_notes text; m_at timestamptz;
    c_value text; c_at timestamptz; c_notes text;
    legacy_rel text;
    kinds text[];
    row_id uuid;
    n int;
BEGIN
    IF to_regclass('public.company_relationships') IS NULL
       OR (SELECT relkind FROM pg_class WHERE oid = to_regclass('public.company_relationships')) <> 'r' THEN
        RETURN;
    END IF;
    SELECT * INTO e FROM company_edges WHERE id = _edge;
    kinds := CASE WHEN e.relationship = 'competitor' THEN ARRAY['competitor', 'suspected_competitor']
                  ELSE ARRAY[e.relationship] END;
    own := own_company();
    IF own IS NULL THEN
        -- Without a recorded own company the door cannot tell whether the old
        -- table mirrors this link. Refuse rather than let the two drift.
        IF EXISTS (SELECT 1 FROM company_relationships
                    WHERE company_id = e.from_company_id AND relationship = ANY (kinds)) THEN
            RAISE EXCEPTION 'this instance has no own company recorded (client/own_company = primary) and the old '
                            'table holds a row for this company; record the own company (ticket 02) before '
                            'writing through the door'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN;
    END IF;
    IF e.to_company_id <> own THEN
        RETURN;
    END IF;

    SELECT id, confidence, decided_by, asserted_by, notes, asserted_at
      INTO m_id, m_confidence, m_decided_by, m_asserted_by, m_notes, m_at
      FROM assertions
     WHERE subject_company_edge_id = _edge AND namespace = 'relationship' AND key = e.relationship
       AND removed_at IS NULL;
    IF m_id IS NULL THEN
        RETURN;
    END IF;
    legacy_rel := CASE WHEN e.relationship = 'competitor' AND m_confidence = 'suspected'
                       THEN 'suspected_competitor' ELSE e.relationship END;

    SELECT count(*) INTO n FROM company_relationships
     WHERE company_id = e.from_company_id AND relationship = ANY (kinds);
    IF n > 1 THEN
        RAISE EXCEPTION 'the old table holds % rows of kinds % for company %; resolve by hand first',
            n, kinds, e.from_company_id USING ERRCODE = 'check_violation';
    END IF;
    SELECT id INTO row_id FROM company_relationships
     WHERE company_id = e.from_company_id AND relationship = ANY (kinds);
    IF row_id IS NULL THEN
        INSERT INTO company_relationships (company_id, relationship, source, notes, recorded_at)
        VALUES (e.from_company_id, legacy_rel, coalesce(m_decided_by, m_asserted_by), m_notes, m_at)
        RETURNING id INTO row_id;
    END IF;

    IF _fact = 'relationship' THEN
        UPDATE company_relationships SET relationship = legacy_rel WHERE id = row_id;
    ELSIF _fact IN ('scope', 'status') THEN
        SELECT value INTO c_value FROM assertions
         WHERE subject_company_edge_id = _edge AND namespace = 'relationship' AND key = _fact AND removed_at IS NULL;
        IF _fact = 'scope' THEN
            UPDATE company_relationships SET scope = c_value WHERE id = row_id;
        ELSE
            UPDATE company_relationships SET status = c_value WHERE id = row_id;
        END IF;
    ELSIF _fact = 'monitor' THEN
        SELECT value, coalesce(decided_at, asserted_at) INTO c_value, c_at FROM assertions
         WHERE subject_company_edge_id = _edge AND namespace = 'monitoring' AND key = 'monitor' AND removed_at IS NULL;
        UPDATE company_relationships
           SET monitor_desired = coalesce(c_value = 'monitor', false), monitor_desired_at = c_at
         WHERE id = row_id;
    ELSIF _fact = 'client_choice' THEN
        SELECT value, coalesce(decided_at, asserted_at), notes INTO c_value, c_at, c_notes FROM assertions
         WHERE subject_company_edge_id = _edge AND namespace = 'client' AND key = 'choice' AND removed_at IS NULL;
        UPDATE company_relationships
           SET client_disposition = c_value, client_disposition_at = c_at::date,
               client_disposition_source = c_notes
         WHERE id = row_id;
    ELSE
        RAISE EXCEPTION 'company_relationship_legacy_sync: unknown fact %', _fact USING ERRCODE = 'check_violation';
    END IF;
END$$;

-- ── the door ───────────────────────────────────────────────────────────────
-- Create or change a link: its type, whether it is only a possible competitor,
-- and how close. _scope 'none' clears the scope. Returns the link id.
CREATE OR REPLACE FUNCTION set_company_relationship(
    _from uuid, _to uuid, _relationship text, _asserted_by text, _notes text,
    _scope text DEFAULT NULL, _possible boolean DEFAULT false,
    _decided_by text DEFAULT NULL, _via text DEFAULT NULL
) RETURNS uuid
LANGUAGE plpgsql AS $$
DECLARE
    e uuid;
    r jsonb;
BEGIN
    IF _possible AND _relationship <> 'competitor' THEN
        RAISE EXCEPTION 'only a competitor link can be marked possible' USING ERRCODE = 'check_violation';
    END IF;
    IF _scope IS NOT NULL AND _scope NOT IN ('direct', 'adjacent', 'aspirational', 'none') THEN
        RAISE EXCEPTION 'scope must be direct, adjacent, aspirational or none (got %)', _scope
            USING ERRCODE = 'check_violation';
    END IF;
    e := company_link(_from, _to, _relationship);
    r := link_assert(e, 'relationship', _relationship, NULL, _asserted_by, _notes, _decided_by, _via, NULL,
                     CASE WHEN _possible THEN 'suspected' END);
    PERFORM company_relationship_legacy_sync(e, 'relationship');
    IF _scope = 'none' THEN
        UPDATE assertions SET removed_at = now(), removed_by = _asserted_by,
                              removed_reason = 'scope cleared: ' || coalesce(_notes, '')
         WHERE subject_company_edge_id = e AND namespace = 'relationship' AND key = 'scope' AND removed_at IS NULL;
        PERFORM company_relationship_legacy_sync(e, 'scope');
    ELSIF _scope IS NOT NULL THEN
        PERFORM link_assert(e, 'relationship', 'scope', _scope, _asserted_by, _notes, _decided_by, _via);
        PERFORM company_relationship_legacy_sync(e, 'scope');
    END IF;
    RETURN e;
END$$;

-- A decision about a live link. _fact: monitor | client_choice | leave_out | status.
CREATE OR REPLACE FUNCTION set_link_decision(
    _edge uuid, _fact text, _value text, _decided_by text, _via text, _notes text, _asserted_by text,
    _decided_at timestamptz DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    ns text;
    k text;
    allowed text[];
    r jsonb;
BEGIN
    CASE _fact
        WHEN 'monitor'       THEN ns := 'monitoring';   k := 'monitor';   allowed := ARRAY['monitor', 'do_not_monitor'];
        WHEN 'client_choice' THEN ns := 'client';       k := 'choice';    allowed := ARRAY['selected', 'deselected', 'not_offered'];
        WHEN 'leave_out'     THEN ns := 'scan';         k := 'leave_out'; allowed := ARRAY['leave_out'];
        WHEN 'status'        THEN ns := 'relationship'; k := 'status';    allowed := ARRAY['clear', 'conflicting', 'checking'];
        ELSE RAISE EXCEPTION 'unknown fact % (monitor, client_choice, leave_out, status)', _fact
                 USING ERRCODE = 'check_violation';
    END CASE;
    IF _value IS NULL OR NOT (_value = ANY (allowed)) THEN
        RAISE EXCEPTION '% must be one of % (got %)', _fact, allowed, _value USING ERRCODE = 'check_violation';
    END IF;
    IF NOT link_is_live(_edge) THEN
        RAISE EXCEPTION 'link % is not live (withdrawn, or never recorded); record the relationship first', _edge
            USING ERRCODE = 'check_violation';
    END IF;
    r := link_assert(_edge, ns, k, _value, _asserted_by, _notes, _decided_by, _via, _decided_at);
    IF _fact <> 'leave_out' THEN
        PERFORM company_relationship_legacy_sync(_edge, _fact);
    END IF;
    RETURN r;
END$$;

-- Stop monitoring a company's sellers and keep leaving it out of scans.
CREATE OR REPLACE FUNCTION stop_monitoring(
    _edge uuid, _decided_by text, _via text, _notes text, _asserted_by text
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    m jsonb;
    l jsonb;
BEGIN
    m := set_link_decision(_edge, 'monitor', 'do_not_monitor', _decided_by, _via, _notes, _asserted_by);
    IF NOT EXISTS (SELECT 1 FROM assertions WHERE subject_company_edge_id = _edge AND namespace = 'scan'
                     AND key = 'leave_out' AND removed_at IS NULL) THEN
        l := set_link_decision(_edge, 'leave_out', 'leave_out', _decided_by, _via,
                               'kept on the left-out list when monitoring stopped: ' || _notes, _asserted_by);
    END IF;
    RETURN jsonb_build_object('monitor', m, 'leave_out', coalesce(l, '"already on the list"'::jsonb));
END$$;

-- Added by mistake: close every live claim on the link. The link row and every
-- claim stay; the link is simply no longer live. Returns how many were closed.
CREATE OR REPLACE FUNCTION withdraw_company_link(
    _edge uuid, _decided_by text, _notes text, _asserted_by text
) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE
    e company_edges%ROWTYPE;
    n int;
BEGIN
    IF _decided_by IS NULL OR btrim(_decided_by) = '' OR _notes IS NULL OR btrim(_notes) = ''
       OR _asserted_by IS NULL OR btrim(_asserted_by) = '' THEN
        RAISE EXCEPTION 'withdrawing a link needs decided_by, a reason and asserted_by' USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO e FROM company_edges WHERE id = _edge;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no company link %', _edge USING ERRCODE = 'check_violation';
    END IF;
    IF to_regclass('public.company_relationships') IS NOT NULL
       AND (SELECT relkind FROM pg_class WHERE oid = to_regclass('public.company_relationships')) = 'r'
       AND e.to_company_id IS NOT DISTINCT FROM own_company()
       AND EXISTS (SELECT 1 FROM company_relationships
                    WHERE company_id = e.from_company_id
                      AND relationship = ANY (CASE WHEN e.relationship = 'competitor'
                                                   THEN ARRAY['competitor', 'suspected_competitor']
                                                   ELSE ARRAY[e.relationship] END)) THEN
        RAISE EXCEPTION 'this link is mirrored by a row of the old company_relationships table, which cannot say '
                        '"withdrawn" without a delete; withdrawing it becomes possible once that table is a view '
                        '(ticket 04)' USING ERRCODE = 'check_violation';
    END IF;
    UPDATE assertions
       SET removed_at = now(), removed_by = _asserted_by,
           removed_reason = 'added by mistake: ' || _notes || ' (decided by ' || _decided_by || ')'
     WHERE subject_company_edge_id = _edge AND removed_at IS NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END$$;

-- ── reading ────────────────────────────────────────────────────────────────
-- Current state: one row per LIVE link.
CREATE OR REPLACE VIEW company_link_state AS
SELECT e.id AS link_id, e.from_company_id, e.to_company_id, e.relationship,
       (mk.confidence = 'suspected') AS possible,
       mk.asserted_at AS recorded_at, mk.asserted_by AS recorded_by, mk.decided_by AS recorded_decided_by,
       mk.notes AS recorded_notes,
       (SELECT min(a.asserted_at) FROM assertions a
         WHERE a.subject_company_edge_id = e.id AND a.namespace = 'relationship' AND a.key = e.relationship)
           AS first_recorded_at,
       sc.value AS scope, st.value AS status,
       mo.value AS monitor, mo.decided_by AS monitor_decided_by, mo.notes AS monitor_reason,
       coalesce(mo.decided_at, mo.asserted_at) AS monitor_at,
       ch.value AS client_choice, ch.decided_by AS client_choice_by, ch.notes AS client_choice_source,
       coalesce(ch.decided_at, ch.asserted_at) AS client_choice_at,
       (lo.id IS NOT NULL) AS leave_out, lo.decided_by AS leave_out_decided_by,
       coalesce(rd.value, 'page_sweep') AS rep_discovery, dc.value AS discovery_config
  FROM company_edges e
  JOIN assertions mk ON mk.subject_company_edge_id = e.id AND mk.namespace = 'relationship'
                    AND mk.key = e.relationship AND mk.removed_at IS NULL
  LEFT JOIN assertions sc ON sc.subject_company_edge_id = e.id AND sc.namespace = 'relationship' AND sc.key = 'scope' AND sc.removed_at IS NULL
  LEFT JOIN assertions st ON st.subject_company_edge_id = e.id AND st.namespace = 'relationship' AND st.key = 'status' AND st.removed_at IS NULL
  LEFT JOIN assertions mo ON mo.subject_company_edge_id = e.id AND mo.namespace = 'monitoring' AND mo.key = 'monitor' AND mo.removed_at IS NULL
  LEFT JOIN assertions ch ON ch.subject_company_edge_id = e.id AND ch.namespace = 'client' AND ch.key = 'choice' AND ch.removed_at IS NULL
  LEFT JOIN assertions lo ON lo.subject_company_edge_id = e.id AND lo.namespace = 'scan' AND lo.key = 'leave_out' AND lo.removed_at IS NULL
  LEFT JOIN assertions rd ON rd.subject_company_edge_id = e.id AND rd.namespace = 'discovery' AND rd.key = 'rep_discovery' AND rd.removed_at IS NULL
  LEFT JOIN assertions dc ON dc.subject_company_edge_id = e.id AND dc.namespace = 'discovery' AND dc.key = 'config' AND dc.removed_at IS NULL;

COMMENT ON VIEW company_link_state IS
    'Current state of every live company link (102): one row per link, each fact from its active assertion. '
    'History: company_link_history.';

-- History: every claim ever made about every link, newest first per link.
CREATE OR REPLACE VIEW company_link_history AS
SELECT e.id AS link_id, e.from_company_id, e.to_company_id, e.relationship,
       a.id AS assertion_id, a.namespace, a.key, a.value, a.confidence, a.kind, a.via, a.decided_by, a.decided_at,
       a.notes, a.asserted_by, a.asserted_at, a.removed_by, a.removed_at, a.removed_reason
  FROM company_edges e
  JOIN assertions a ON a.subject_company_edge_id = e.id;

-- The new format in the old table's shape: links to the primary own company.
-- The conversion (02) and the swap (04) compare this with company_relationships.
CREATE OR REPLACE VIEW v_company_relationships_from_links AS
SELECT s.link_id,
       s.from_company_id AS company_id,
       CASE WHEN s.relationship = 'competitor' AND s.possible THEN 'suspected_competitor' ELSE s.relationship END
           AS relationship,
       s.scope,
       coalesce(s.recorded_decided_by, s.recorded_by) AS source,
       s.recorded_notes AS notes,
       s.first_recorded_at AS recorded_at,
       s.status,
       coalesce(s.monitor = 'monitor', false) AS monitor_desired,
       s.monitor_at AS monitor_desired_at,
       s.rep_discovery,
       s.discovery_config::jsonb AS discovery_config,
       s.client_choice AS client_disposition,
       s.client_choice_at::date AS client_disposition_at,
       s.client_choice_source AS client_disposition_source
  FROM company_link_state s
 WHERE s.to_company_id = own_company();
