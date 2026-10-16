-- Make `lead_identities.kind` extensible: a lookup table plus a foreign key,
-- mirroring the `lead_sources.source_type -> source_types` pattern that this
-- same schema already uses for exactly the same problem.
--
-- The defect: `kind` was pinned by a CHECK listing eight literals, so adding an
-- identity type -- a CRM contact id, a dialer prospect id -- was a SCHEMA change
-- that had to propagate to every instance, while adding a lead SOURCE type is an
-- INSERT. One vocabulary treated as data, the other as structure, with no reason
-- for the difference. This adopts the data answer.
--
-- Widening only, and deliberately so: the lookup is seeded with every value the
-- CHECK allowed AND every value already present in the table, so the new FK
-- cannot reject a row that was legal a moment ago. Surveyed on all six instances
-- before writing -- kinds in use are email, linkedin_url, linkedin_urn and
-- whatsapp, and the constraint is named `ck_lead_identities_kind` on every one.
--
-- Note for callers: a lead may hold SEVERAL identities of one kind. The only
-- unique index is (kind, value) -- which keeps one value from pointing at two
-- leads -- and `is_primary` disambiguates. A lead with two emails already exists.

CREATE TABLE IF NOT EXISTS lead_identity_kinds (
  name        text PRIMARY KEY,
  description text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- The eight the CHECK allowed, plus the one generic kind this migration ships.
INSERT INTO lead_identity_kinds (name, description) VALUES
  ('linkedin_urn',      'LinkedIn member URN -- the stable numeric identity behind a profile.'),
  ('linkedin_url',      'Canonical LinkedIn profile URL.'),
  ('email',             'Email address.'),
  ('phone',             'Telephone number.'),
  ('whatsapp',          'WhatsApp-reachable number, as used by the conversation sync.'),
  ('x_handle',          'Handle on X.'),
  ('github',            'GitHub username.'),
  ('domain',            'Domain owned by or associated with the person.'),
  ('crm_contact_id',    'Contact id in the client CRM. Lets a CRM-sourced person be '
                        'resolved to a lead by id instead of by name or email, which '
                        'is the only form of that lookup that cannot collide.')
ON CONFLICT (name) DO NOTHING;

-- Tool-specific kinds are NOT seeded here. Once `kind` is a lookup row rather
-- than a CHECK literal, a per-instance vocabulary entry is an INSERT against
-- that instance -- which is both the point of this migration and the reason no
-- vendor name needs to enter this repository to add one.

-- Adopt anything an instance already holds that the list above does not cover:
-- a restore, or a database that predates the CHECK, must not fail the FK below.
INSERT INTO lead_identity_kinds (name, description)
SELECT DISTINCT kind, 'Pre-existing kind adopted by migration 058.'
FROM lead_identities
ON CONFLICT (name) DO NOTHING;

-- Retire the closed enum. Verified present under this name on all six instances.
ALTER TABLE lead_identities DROP CONSTRAINT IF EXISTS ck_lead_identities_kind;

-- ADD CONSTRAINT has no IF NOT EXISTS; guard it so a re-run is a no-op.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'lead_identities'::regclass
      AND conname  = 'fk_lead_identities_kind'
  ) THEN
    ALTER TABLE lead_identities
      ADD CONSTRAINT fk_lead_identities_kind
      FOREIGN KEY (kind) REFERENCES lead_identity_kinds(name) ON UPDATE CASCADE;
  END IF;
END $$;

COMMENT ON TABLE lead_identity_kinds IS
  'Vocabulary for lead_identities.kind. Adding an identity type is an INSERT '
  'here, not a migration -- the same contract as source_types. Never DELETE a '
  'row that lead_identities references.';
