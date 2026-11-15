-- 097: speakers at conferences and podcasts, found on the public web.
--
-- Events and who belongs to them already have a home (096): a conference
-- series or a podcast show is a `community`, each edition or episode is an
-- `event`, and a speaker, host or guest is a membership with that role. This
-- migration adds only what 096 lacks for people read off a public page:
--
--   event_speaker_page     an identity kind. A speaker page usually gives a
--                          name, a title and a company, and no profile URL.
--                          A name is never a key, so the person is keyed by
--                          their own entry on the event's site (its link, or
--                          the event platform's speaker id) until an exact
--                          identifier such as a profile URL is found. The same
--                          person at two events is two people until then;
--                          same-name pairs go to lead_possible_matches.
--   speaker_search_policy  what to search for and how far to look, per topic
--                          set, versioned like voice_policy. Rules live in the
--                          instance, never in a file.
--   enrichment_source_policy rows for the three free sources the finder reads.

INSERT INTO lead_identity_kinds (name, description) VALUES
  ('event_speaker_page', 'A speaker''s own entry on an event or show site: the link to it, or the event '
                         'platform''s speaker id. Scoped to that site, so it never joins two events; a '
                         'profile URL found later does that.')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS speaker_search_policy (
    topic_set       text        NOT NULL,
    version         text        NOT NULL,
    search_terms    text[]      NOT NULL CHECK (cardinality(search_terms) > 0),
    months_back     integer     NOT NULL CHECK (months_back >= 0),
    months_ahead    integer     NOT NULL CHECK (months_ahead >= 0),
    source          text        NOT NULL,
    rationale       text        NOT NULL,
    created_by      text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    superseded_at   timestamptz,
    PRIMARY KEY (topic_set, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_speaker_search_policy_active
    ON speaker_search_policy (topic_set) WHERE superseded_at IS NULL;

COMMENT ON TABLE speaker_search_policy IS
    'What the speaker finder searches for, per topic set, versioned. One active row per topic set; a change is a new version and the old row is marked superseded.';
COMMENT ON COLUMN speaker_search_policy.search_terms IS
    'The phrases searched for conferences and shows on this topic.';
COMMENT ON COLUMN speaker_search_policy.months_back IS
    'How far back an event or episode may be dated and still be collected.';
COMMENT ON COLUMN speaker_search_policy.months_ahead IS
    'How far ahead an announced event may be dated and still be collected.';
COMMENT ON COLUMN speaker_search_policy.source IS
    'Where the rule came from, citable: a person and a date, a call, a doc.';

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('web', 'page', 30,
   'A public event, speaker or episode page read by the speaker finder, keyed by its URL. Line-ups '
   'change slowly once announced, so 30 days.'),
  ('agent_search', 'event_discovery', 30,
   'What a web search for conferences and shows on a topic returned, keyed by the query, the topic '
   'set and the window. Re-searched after 30 days.'),
  ('sessionize', '/api/v2/{id}/view/Speakers', 30,
   'An event''s public speaker feed on Sessionize, keyed by the event id. Same clock as a speaker page.')
ON CONFLICT (api, endpoint) DO NOTHING;
