-- 103_company_relationship_conversion.sql
-- Converting the old company_relationships rows into the company-link format (102).
--
-- What 102 left for the conversion:
--
--   * The relationship types instances actually hold beyond competitor/other
--     (security vendors, the house instance's prospect stages, adjacent
--     technologies) were not registered, so the door refused them.
--   * monitoring/monitor, client/choice and relationship/status were
--     registered as decisions only. The old rows hold many values nobody is
--     recorded as having decided (a monitor_desired set by a research tool, a
--     status from a vetting sheet, a not_offered computed from a workbook's
--     absences). Writing those as decisions would invent a decider. They are
--     now registered with no fixed kind: the door still writes only decisions
--     (set_link_decision now refuses a missing decider itself), and the
--     conversion writes an unclassified claim where the old row recorded no
--     decider.
--   * The instance's own company ('self' rows) becomes a claim on the company,
--     not a link (a company is not linked to itself), and the old-shaped view
--     shows it again as a 'self' row.
--
-- The conversion itself is a database function, so CI can test it on
-- synthetic rows: convert_company_relationship(row id, writer, client label).
-- It never changes or deletes the old row; it adds claims, carrying the old
-- row's writer (`source`), notes and dates. Running it twice writes nothing the
-- second time.
--
-- Provenance rules (settled with the operator 2026-11-25):
--   * marker: writer = the row's source, notes = its notes, date = recorded_at.
--   * monitor: a company-level monitoring/monitor DECISION on file is copied
--     onto the link as a decision (the company-level one stays active until its
--     two readers move, ticket 03). Otherwise a true monitor_desired, or a
--     false one with a date (a deliberate setting), becomes an UNCLASSIFIED
--     claim; a false with no date is the column default and writes nothing.
--     A false never becomes a do_not_monitor decision.
--   * client choice: selected / deselected were the client's decision (they
--     reviewed a workbook) and cite client_disposition_source; not_offered is
--     an absence, never a decision, and travels unclassified.
--   * status, scope, rep_discovery and discovery_config travel unchanged,
--     unclassified.

-- ── registry ───────────────────────────────────────────────────────────────
INSERT INTO tag_definitions (namespace, key, entity_type, layer, description, kind) VALUES
  ('relationship', 'security_vendor', 'company_edge', 'relationship',
   'The from-company sells security products the to-company''s buyers also buy (an inferred landscape entry). Live marker.', NULL),
  ('relationship', 'prospect', 'company_edge', 'relationship',
   'The from-company is a prospect of the to-company. Live marker.', NULL),
  ('relationship', 'prospect_candidate', 'company_edge', 'relationship',
   'The from-company is a candidate prospect of the to-company, not yet qualified. Live marker.', NULL),
  ('relationship', 'prospect_deferred', 'company_edge', 'relationship',
   'The from-company was a prospect of the to-company and is deferred. Live marker.', NULL),
  ('relationship', 'adjacent_technology', 'company_edge', 'relationship',
   'The from-company sells a technology next to the to-company''s market. Live marker.', NULL)
ON CONFLICT (namespace, key, entity_type) DO UPDATE
   SET description = EXCLUDED.description, layer = EXCLUDED.layer, kind = EXCLUDED.kind;

UPDATE tag_definitions SET kind = NULL
 WHERE entity_type = 'company_edge'
   AND (namespace, key) IN (('monitoring', 'monitor'), ('client', 'choice'), ('relationship', 'status'));

-- ── the door still writes decisions only ───────────────────────────────────
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
    IF _decided_by IS NULL OR btrim(_decided_by) = '' THEN
        RAISE EXCEPTION 'set_link_decision: % is a decision and needs decided_by', _fact USING ERRCODE = 'check_violation';
    END IF;
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

-- ── the own company ────────────────────────────────────────────────────────
-- Mark a company as the instance's own (primary) or another own brand. An
-- identical mark is a no-op; a second primary is refused.
CREATE OR REPLACE FUNCTION set_own_company(
    _company uuid, _value text, _asserted_by text, _notes text, _asserted_at timestamptz DEFAULT NULL
) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    cur_value text;
BEGIN
    IF _value NOT IN ('primary', 'brand') THEN
        RAISE EXCEPTION 'own company must be primary or brand (got %)', _value USING ERRCODE = 'check_violation';
    END IF;
    IF _asserted_by IS NULL OR btrim(_asserted_by) = '' THEN
        RAISE EXCEPTION 'set_own_company: asserted_by is required' USING ERRCODE = 'check_violation';
    END IF;
    PERFORM 1 FROM companies WHERE id = _company;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no company %', _company USING ERRCODE = 'check_violation';
    END IF;
    SELECT value INTO cur_value FROM assertions
     WHERE subject_company_id = _company AND namespace = 'client' AND key = 'own_company' AND removed_at IS NULL;
    IF cur_value IS NOT DISTINCT FROM _value THEN
        RETURN 'unchanged';
    END IF;
    IF cur_value IS NOT NULL THEN
        RAISE EXCEPTION 'company % is already marked own (%); change it by hand with a reason', _company, cur_value
            USING ERRCODE = 'check_violation';
    END IF;
    IF _value = 'primary' AND own_company() IS NOT NULL THEN
        RAISE EXCEPTION 'this instance already has a primary own company (%)', own_company()
            USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO assertions (subject_company_id, namespace, key, value, notes, asserted_by, asserted_at)
    VALUES (_company, 'client', 'own_company', _value, _notes, _asserted_by, coalesce(_asserted_at, now()));
    RETURN 'inserted';
END$$;

-- ── converting one old row ─────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION convert_company_relationship(
    _row uuid, _asserted_by text, _client_label text
) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
    r company_relationships%ROWTYPE;
    own uuid;
    kind_of_link text;
    e uuid;
    written text[] := ARRAY[]::text[];
    conv_note text;
    d_id uuid; d_value text; d_by text; d_via text; d_at timestamptz; d_notes text; d_asserted_by text; d_asserted_at timestamptz;
BEGIN
    IF _asserted_by IS NULL OR btrim(_asserted_by) = '' OR _client_label IS NULL OR btrim(_client_label) = '' THEN
        RAISE EXCEPTION 'convert_company_relationship needs the writer and the client label' USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO r FROM company_relationships WHERE id = _row;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no company_relationships row %', _row USING ERRCODE = 'check_violation';
    END IF;
    conv_note := 'converted from company_relationships row ' || r.id || ' by ' || _asserted_by;

    IF r.relationship = 'self' THEN
        IF EXISTS (SELECT 1 FROM assertions WHERE subject_company_id = r.company_id AND namespace = 'client'
                     AND key = 'own_company' AND removed_at IS NULL) THEN
            RETURN jsonb_build_object('row', r.id, 'outcome', 'already converted');
        END IF;
        RAISE EXCEPTION 'self row % has no own-company mark; mark the own companies first (set_own_company)', r.id
            USING ERRCODE = 'check_violation';
    END IF;

    own := own_company();
    IF own IS NULL THEN
        RAISE EXCEPTION 'this instance has no primary own company; mark it first (set_own_company)'
            USING ERRCODE = 'check_violation';
    END IF;
    IF r.company_id = own THEN
        RAISE EXCEPTION 'row % relates the own company to itself (%); resolve by hand', r.id, r.relationship
            USING ERRCODE = 'check_violation';
    END IF;
    kind_of_link := CASE WHEN r.relationship = 'suspected_competitor' THEN 'competitor' ELSE r.relationship END;
    e := company_link(r.company_id, own, kind_of_link);
    IF link_is_live(e) THEN
        RETURN jsonb_build_object('row', r.id, 'link', e, 'outcome', 'already converted');
    END IF;

    -- the marker carries the old row's writer, notes and date
    INSERT INTO assertions (subject_company_edge_id, namespace, key, confidence, notes, asserted_by, asserted_at)
    VALUES (e, 'relationship', kind_of_link,
            CASE WHEN r.relationship = 'suspected_competitor' THEN 'suspected' END,
            r.notes, r.source, r.recorded_at);
    written := written || 'marker'::text;

    IF r.scope IS NOT NULL THEN
        INSERT INTO assertions (subject_company_edge_id, namespace, key, value, notes, asserted_by, asserted_at)
        VALUES (e, 'relationship', 'scope', r.scope, conv_note, r.source, r.recorded_at);
        written := written || 'scope'::text;
    END IF;
    IF r.status IS NOT NULL THEN
        INSERT INTO assertions (subject_company_edge_id, namespace, key, value, notes, asserted_by, asserted_at)
        VALUES (e, 'relationship', 'status', r.status, conv_note, r.source, r.recorded_at);
        written := written || 'status'::text;
    END IF;

    -- monitor: a decision on file is copied; otherwise only a value someone set
    SELECT id, value, decided_by, via, decided_at, notes, asserted_by, asserted_at
      INTO d_id, d_value, d_by, d_via, d_at, d_notes, d_asserted_by, d_asserted_at
      FROM assertions
     WHERE subject_company_id = r.company_id AND namespace = 'monitoring' AND key = 'monitor'
       AND kind = 'decision' AND removed_at IS NULL;
    IF d_id IS NOT NULL THEN
        IF (d_value = 'monitor') IS DISTINCT FROM r.monitor_desired THEN
            RAISE EXCEPTION 'row %: the monitor decision on file (%) disagrees with monitor_desired (%); resolve by hand',
                r.id, d_value, r.monitor_desired USING ERRCODE = 'check_violation';
        END IF;
        INSERT INTO assertions (subject_company_edge_id, namespace, key, value, kind, decided_by, via, decided_at,
                                notes, asserted_by, asserted_at)
        VALUES (e, 'monitoring', 'monitor', d_value, 'decision', d_by, d_via, d_at,
                d_notes, d_asserted_by, d_asserted_at);
        written := written || 'monitor decision'::text;
    ELSIF r.monitor_desired OR r.monitor_desired_at IS NOT NULL THEN
        INSERT INTO assertions (subject_company_edge_id, namespace, key, value, notes, asserted_by, asserted_at)
        VALUES (e, 'monitoring', 'monitor', CASE WHEN r.monitor_desired THEN 'monitor' ELSE 'do_not_monitor' END,
                conv_note || '; no decider recorded', r.source, coalesce(r.monitor_desired_at, r.recorded_at));
        written := written || 'monitor'::text;
    END IF;

    -- client choice
    IF r.client_disposition IN ('selected', 'deselected') THEN
        INSERT INTO assertions (subject_company_edge_id, namespace, key, value, kind, decided_by, via, decided_at,
                                notes, asserted_by)
        VALUES (e, 'client', 'choice', r.client_disposition, 'decision', _client_label || ' (the client)',
                'reviewed_batch', r.client_disposition_at::timestamptz, r.client_disposition_source, _asserted_by);
        written := written || 'client choice decision'::text;
    ELSIF r.client_disposition IS NOT NULL THEN
        INSERT INTO assertions (subject_company_edge_id, namespace, key, value, notes, asserted_by, asserted_at)
        VALUES (e, 'client', 'choice', r.client_disposition, r.client_disposition_source, _asserted_by,
                coalesce(r.client_disposition_at::timestamptz, now()));
        written := written || 'client choice'::text;
    END IF;

    IF r.rep_discovery IS DISTINCT FROM 'page_sweep' THEN
        INSERT INTO assertions (subject_company_edge_id, namespace, key, value, notes, asserted_by, asserted_at)
        VALUES (e, 'discovery', 'rep_discovery', r.rep_discovery, conv_note, r.source, r.recorded_at);
        written := written || 'rep_discovery'::text;
    END IF;
    IF r.discovery_config IS NOT NULL THEN
        INSERT INTO assertions (subject_company_edge_id, namespace, key, value, notes, asserted_by, asserted_at)
        VALUES (e, 'discovery', 'config', r.discovery_config::text, conv_note, r.source, r.recorded_at);
        written := written || 'discovery_config'::text;
    END IF;

    RETURN jsonb_build_object('row', r.id, 'link', e, 'outcome', 'converted', 'written', to_jsonb(written));
END$$;

-- Links written before markers existed (the house instance's competitor links
-- carry scope and evidence claims but no marker) get one, so company_link_state
-- shows them. Writer and date come from the link's earliest claim.
CREATE OR REPLACE FUNCTION mark_unmarked_link(_edge uuid, _asserted_by text) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    e company_edges%ROWTYPE;
    first_by text;
BEGIN
    SELECT * INTO e FROM company_edges WHERE id = _edge;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no company link %', _edge USING ERRCODE = 'check_violation';
    END IF;
    IF link_is_live(_edge) THEN
        RETURN 'already marked';
    END IF;
    IF EXISTS (SELECT 1 FROM assertions WHERE subject_company_edge_id = _edge AND removed_at IS NOT NULL
                 AND removed_reason LIKE 'added by mistake:%') THEN
        RETURN 'withdrawn, left alone';
    END IF;
    SELECT asserted_by INTO first_by FROM assertions
     WHERE subject_company_edge_id = _edge AND removed_at IS NULL ORDER BY asserted_at, id LIMIT 1;
    IF first_by IS NULL THEN
        RETURN 'no live claims, left alone';
    END IF;
    PERFORM 1 FROM tag_definitions WHERE namespace = 'relationship' AND key = e.relationship AND entity_type = 'company_edge';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'relationship % is not registered', e.relationship USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO assertions (subject_company_edge_id, namespace, key, notes, asserted_by, asserted_at)
    VALUES (_edge, 'relationship', e.relationship,
            'marker added by ' || _asserted_by || ': the link predates markers; writer and date from its first claim',
            first_by, e.first_seen_at);
    RETURN 'marked';
END$$;

-- ── the old table's shape, own companies included ──────────────────────────
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
 WHERE s.to_company_id = own_company()
UNION ALL
SELECT NULL::uuid AS link_id,
       a.subject_company_id AS company_id,
       'self' AS relationship,
       NULL::text AS scope,
       a.asserted_by AS source,
       a.notes,
       a.asserted_at AS recorded_at,
       NULL::text AS status,
       false AS monitor_desired,
       NULL::timestamptz AS monitor_desired_at,
       'page_sweep' AS rep_discovery,
       NULL::jsonb AS discovery_config,
       NULL::text AS client_disposition,
       NULL::date AS client_disposition_at,
       NULL::text AS client_disposition_source
  FROM assertions a
 WHERE a.namespace = 'client' AND a.key = 'own_company' AND a.subject_company_id IS NOT NULL
   AND a.removed_at IS NULL;
