-- 096: collectives — the groups, communities and events people belong to.
--
-- Until now "who belongs to what" lived in three unrelated places: an event was a
-- note on each guest's lead_sources row, every event of a series shared one
-- audience tag, and a messaging group's members (094) were not linked to people
-- at all. None of them could answer "who applied to this event and never checked
-- in", "what is this person a member of", or "everyone in anything this
-- community runs".
--
--   collectives                    one row per group, community or event. A
--                                  community is the parent of its groups and
--                                  events. A collective may point at the company
--                                  behind it; its members are never employees.
--   collective_memberships         one row per (collective, member). Three
--                                  separate facts, because sources mix them up:
--                                  the application outcome, attendance, and for
--                                  a group whether the person is still in it.
--                                  The source's own wording is kept beside them.
--   collective_membership_changes  every change of those facts, dated, written
--                                  by trigger. The membership row is the latest.
--   lead_possible_matches          "maybe the same person": two identities that
--                                  share a name in a shared context. Never
--                                  merged on that evidence; an exact shared
--                                  identifier later proves the pair.
--
-- Members are keyed by an IDENTITY (lead_identities kind + value), not by a
-- lead id, as 094 keys chat members by the network's own id. The identity
-- resolves to the person through lead_identities, which merge_lead() already
-- moves to the keeper, so a merge needs no new column in its registry and a
-- membership can never point at a merged-away row. v_collective_members does
-- the resolving.
--
-- Also here: a way to retire a tag so that writing it fails with an explanation
-- (tag_definitions.retired_message, refused by trigger on lead_tags), and
-- live_tag(), which a reader wraps around a tag name to fail the same way.

INSERT INTO lead_identity_kinds (name, description) VALUES
  ('whatsapp_lid', 'WhatsApp''s own member id (the @lid form). Present for every group member, '
                   'including those whose number the network hides.')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS collectives (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind                text NOT NULL CHECK (kind IN ('group', 'community', 'event')),
    name                text,
    provider            text NOT NULL,                 -- whatsapp, luma, partiful, crm, manual, ...
    provider_id         text NOT NULL,                 -- the provider's own id for it
    -- a community holds its groups and events
    parent_id           uuid REFERENCES collectives(id),
    parent_source       text,                          -- inferred | decided
    parent_decided_by   text,
    parent_decided_at   timestamptz,
    -- the company behind it, where there is one
    company_id          uuid REFERENCES companies(id),
    company_source      text,                          -- inferred | decided
    company_decided_by  text,
    company_decided_at  timestamptz,
    -- an event's own facts
    starts_at           timestamptz,
    ends_at             timestamptz,
    location            text,
    url                 text,
    operator_chat_id    uuid REFERENCES operator_chats(id),   -- a messaging group's chat (094)
    source              text NOT NULL,                 -- the tool or import that created the row
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    raw                 jsonb,
    CONSTRAINT uq_collectives_provider UNIQUE (provider, provider_id),
    CONSTRAINT collectives_not_own_parent CHECK (parent_id IS DISTINCT FROM id)
);
CREATE INDEX IF NOT EXISTS idx_collectives_parent  ON collectives (parent_id);
CREATE INDEX IF NOT EXISTS idx_collectives_company ON collectives (company_id);

CREATE TABLE IF NOT EXISTS collective_memberships (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    collective_id   uuid NOT NULL REFERENCES collectives(id),
    member_kind     text NOT NULL REFERENCES lead_identity_kinds(name),
    member_value    text NOT NULL,
    role            text NOT NULL DEFAULT 'member'
                    CHECK (role IN ('member', 'admin', 'host', 'guest', 'speaker')),
    application     text CHECK (application IN ('applied', 'approved', 'declined', 'waitlisted', 'invited')),
    attended        boolean,                           -- NULL = not known or not applicable
    attended_at     timestamptz,
    member_state    text CHECK (member_state IN ('member', 'left')),
    source_status   text,                              -- the source's own wording
    source          text NOT NULL,                     -- the sync or import that observed it
    source_ref      text,                              -- the raw record it came from
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    raw             jsonb,
    CONSTRAINT uq_collective_memberships UNIQUE (collective_id, member_kind, member_value)
);
CREATE INDEX IF NOT EXISTS idx_collective_memberships_member ON collective_memberships (member_kind, member_value);

CREATE TABLE IF NOT EXISTS collective_membership_changes (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    membership_id   uuid NOT NULL REFERENCES collective_memberships(id),
    field           text NOT NULL,                     -- role | application | attended | member_state
    old_value       text,
    new_value       text,
    observed_at     timestamptz NOT NULL DEFAULT now(),
    source          text
);
CREATE INDEX IF NOT EXISTS idx_collective_membership_changes ON collective_membership_changes (membership_id, observed_at);

CREATE OR REPLACE FUNCTION collective_membership_log() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    f text;
    o text;
    n text;
BEGIN
    FOREACH f IN ARRAY ARRAY['role', 'application', 'attended', 'member_state'] LOOP
        n := to_jsonb(NEW) ->> f;
        o := CASE WHEN TG_OP = 'UPDATE' THEN to_jsonb(OLD) ->> f END;
        IF (TG_OP = 'INSERT' AND n IS NOT NULL) OR (TG_OP = 'UPDATE' AND n IS DISTINCT FROM o) THEN
            INSERT INTO collective_membership_changes (membership_id, field, old_value, new_value, source)
            VALUES (NEW.id, f, o, n, NEW.source);
        END IF;
    END LOOP;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_collective_membership_log ON collective_memberships;
CREATE TRIGGER trg_collective_membership_log
    AFTER INSERT OR UPDATE ON collective_memberships
    FOR EACH ROW EXECUTE FUNCTION collective_membership_log();

-- The member list as people. A membership whose identity no person holds yet
-- still shows, with a NULL lead_id.
CREATE OR REPLACE VIEW v_collective_members AS
SELECT m.id AS membership_id, m.collective_id, c.kind, c.name AS collective_name, c.provider, c.parent_id,
       i.lead_id, m.member_kind, m.member_value, m.role, m.application, m.attended, m.attended_at,
       m.member_state, m.source_status, m.source, m.first_seen_at, m.last_seen_at
  FROM collective_memberships m
  JOIN collectives c ON c.id = m.collective_id
  LEFT JOIN lead_identities i ON i.kind = m.member_kind AND i.value = m.member_value;

CREATE TABLE IF NOT EXISTS lead_possible_matches (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    a_kind          text NOT NULL REFERENCES lead_identity_kinds(name),
    a_value         text NOT NULL,
    b_kind          text NOT NULL REFERENCES lead_identity_kinds(name),
    b_value         text NOT NULL,
    basis           text NOT NULL,                     -- e.g. same_name_same_community
    evidence        jsonb NOT NULL,                    -- the name and the shared context
    source          text NOT NULL,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    rejected_by     text,
    rejected_at     timestamptz,
    CONSTRAINT uq_lead_possible_matches UNIQUE (a_kind, a_value, b_kind, b_value),
    CONSTRAINT lead_possible_matches_ordered CHECK ((a_kind, a_value) < (b_kind, b_value))
);

-- open: two different people on file. proven: both identities now belong to one
-- person (an exact identifier joined them). rejected: someone said no.
CREATE OR REPLACE VIEW v_lead_possible_matches AS
SELECT p.id, p.basis, p.evidence, p.source, p.first_seen_at, p.last_seen_at,
       a.lead_id AS a_lead_id, p.a_kind, p.a_value, b.lead_id AS b_lead_id, p.b_kind, p.b_value,
       CASE WHEN p.rejected_at IS NOT NULL THEN 'rejected'
            WHEN a.lead_id IS NOT NULL AND a.lead_id = b.lead_id THEN 'proven'
            ELSE 'open' END AS state
  FROM lead_possible_matches p
  LEFT JOIN lead_identities a ON a.kind = p.a_kind AND a.value = p.a_value
  LEFT JOIN lead_identities b ON b.kind = p.b_kind AND b.value = p.b_value;

-- ── retiring a tag so that using it fails with an explanation ───────────────
ALTER TABLE tag_definitions ADD COLUMN IF NOT EXISTS retired_message text;

CREATE OR REPLACE FUNCTION live_tag(_tag text) RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v text;
BEGIN
    SELECT retired_message INTO v FROM tag_definitions
     WHERE namespace = split_part(_tag, ':', 1) AND key = substr(_tag, strpos(_tag, ':') + 1)
       AND retired_message IS NOT NULL
     LIMIT 1;
    IF v IS NOT NULL THEN
        RAISE EXCEPTION 'tag % is retired: %', _tag, v USING ERRCODE = '23514';
    END IF;
    RETURN _tag;
END $$;

CREATE OR REPLACE FUNCTION refuse_retired_tag() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM live_tag(NEW.tag);
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_lead_tags_refuse_retired ON lead_tags;
CREATE TRIGGER trg_lead_tags_refuse_retired
    BEFORE INSERT ON lead_tags
    FOR EACH ROW EXECUTE FUNCTION refuse_retired_tag();
