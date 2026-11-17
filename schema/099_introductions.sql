-- 099_introductions.sql — who introduced or nominated whom.
--
-- lead_introductions   one row per introduction or nomination:
--     by     the person who made it
--     whom   the person it was about
--     to     who they were introduced to: a person (to_kind + to_value), or an
--            operator of this instance by display name (to_operator)
--     for    what it was for: an event or group (for_collective_id) and/or a
--            purpose in words, such as "speaker"
--   kind introduced   by put whom in touch with the "to" side
--   kind nominated    by suggested whom for something (a talk, an event)
-- Every row says who reported it (source) and when it was recorded. A date the
-- introduction happened is kept when known (happened_on).
--
-- People are keyed by an IDENTITY (lead_identities kind + value), as 096 keys
-- memberships, so merging either person needs no change here. v_lead_introductions
-- resolves the identities to people.
--
-- collective_memberships.application gains 'nominated': someone was suggested for
-- the event or group (as a guest or a speaker) and has not been invited yet.

CREATE TABLE IF NOT EXISTS lead_introductions (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind              text NOT NULL CHECK (kind IN ('introduced', 'nominated')),
    by_kind           text NOT NULL REFERENCES lead_identity_kinds(name),
    by_value          text NOT NULL,
    whom_kind         text NOT NULL REFERENCES lead_identity_kinds(name),
    whom_value        text NOT NULL,
    to_kind           text REFERENCES lead_identity_kinds(name),
    to_value          text,
    to_operator       text,
    for_collective_id uuid REFERENCES collectives(id),
    purpose           text,
    happened_on       date,
    source            text NOT NULL,
    note              text,
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT lead_introductions_to_pair CHECK ((to_kind IS NULL) = (to_value IS NULL)),
    CONSTRAINT lead_introductions_one_to CHECK (to_kind IS NULL OR to_operator IS NULL),
    CONSTRAINT lead_introductions_not_self CHECK ((by_kind, by_value) IS DISTINCT FROM (whom_kind, whom_value)),
    CONSTRAINT uq_lead_introductions UNIQUE NULLS NOT DISTINCT
        (kind, by_kind, by_value, whom_kind, whom_value, to_kind, to_value, to_operator, for_collective_id, purpose)
);

CREATE INDEX IF NOT EXISTS idx_lead_introductions_whom ON lead_introductions (whom_kind, whom_value);
CREATE INDEX IF NOT EXISTS idx_lead_introductions_by ON lead_introductions (by_kind, by_value);

COMMENT ON TABLE lead_introductions IS
    'Who introduced or nominated whom, to whom and for what; people keyed by identity. Read through v_lead_introductions.';
COMMENT ON COLUMN lead_introductions.to_operator IS
    'An outbound_operators display_name, when the person was introduced to an operator of this instance.';
COMMENT ON COLUMN lead_introductions.source IS
    'Who reported the introduction, and where: a person and a date, a call, a message.';

CREATE OR REPLACE VIEW v_lead_introductions AS
SELECT x.id, x.kind,
       b.lead_id AS by_lead_id, lb.name AS by_name,
       w.lead_id AS whom_lead_id, lw.name AS whom_name,
       t.lead_id AS to_lead_id, COALESCE(lt.name, x.to_operator) AS to_name, x.to_operator,
       x.for_collective_id, c.name AS for_collective, x.purpose, x.happened_on, x.source, x.note, x.recorded_at,
       x.by_kind, x.by_value, x.whom_kind, x.whom_value, x.to_kind, x.to_value
  FROM lead_introductions x
  LEFT JOIN lead_identities b ON b.kind = x.by_kind AND b.value = x.by_value
  LEFT JOIN leads lb ON lb.id = b.lead_id
  LEFT JOIN lead_identities w ON w.kind = x.whom_kind AND w.value = x.whom_value
  LEFT JOIN leads lw ON lw.id = w.lead_id
  LEFT JOIN lead_identities t ON t.kind = x.to_kind AND t.value = x.to_value
  LEFT JOIN leads lt ON lt.id = t.lead_id
  LEFT JOIN collectives c ON c.id = x.for_collective_id;

COMMENT ON VIEW v_lead_introductions IS
    'lead_introductions with each identity resolved to its person (by, whom, to) and the event or group named.';

-- ── memberships: a "nominated" step before "invited" ──────────────────────────
ALTER TABLE collective_memberships DROP CONSTRAINT IF EXISTS collective_memberships_application_check;
ALTER TABLE collective_memberships ADD CONSTRAINT collective_memberships_application_check
    CHECK (application IN ('nominated', 'applied', 'approved', 'declined', 'waitlisted', 'invited'));

COMMENT ON COLUMN collective_memberships.application IS
    'Where the person stands with the event or group: nominated (suggested, not yet invited), invited, applied, approved, declined (by either side), waitlisted.';
