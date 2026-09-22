-- 047_enrichment_source_policy.sql
-- How long a cached answer from each enrichment source stays usable.
--
-- Before this table the number lived in whichever caller remembered to pass
-- `max_age_days`, and every caller that forgot got an implicit answer of
-- FOREVER. That is how the employment endpoint — the authority on whether a
-- tracked person still works somewhere, inside tooling built precisely because
-- employment decays — ended up replaying cached successes of unbounded age.
--
-- The failure mode is quiet and worth naming: in one observed run, most of the
-- results reported as verified were replays of a call made weeks earlier, and
-- only a single profile had actually been fetched that day. The report read as
-- a fresh clean bill of health. Nothing in it was false, but nothing in it had
-- been checked either, and there was no way to tell the difference.
--
-- Same argument as tag_definitions: register the source here, WITH the reason,
-- so freshness stops living in magic numbers and chat transcripts.
--
-- `fresh_days` NULL means IMMUTABLE — a point-in-time fact that cannot go
-- stale (who reacted to a post on a given day). NULL is a claim someone made
-- deliberately, which is the whole difference from the old silent infinity.

CREATE TABLE IF NOT EXISTS enrichment_source_policy (
    api        text NOT NULL,
    endpoint   text NOT NULL,
    fresh_days integer,
    rationale  text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (api, endpoint),
    CONSTRAINT enrichment_source_policy_fresh_days_positive
        CHECK (fresh_days IS NULL OR fresh_days > 0)
);

COMMENT ON TABLE enrichment_source_policy IS
    'Per-source cache freshness. api_cache resolves: explicit argument > this '
    'table > no expiry (with a warning). Register a source before relying on '
    'its cache, so "never expires" is a decision rather than an omission.';

COMMENT ON COLUMN enrichment_source_policy.fresh_days IS
    'Days a cached SUCCESS stays usable. NULL = immutable, never expires.';

COMMENT ON COLUMN enrichment_source_policy.rationale IS
    'WHY this number. Required: a bare integer is how the previous guess '
    'survived unreviewed for as long as it did.';

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('saleleads', 'user/experience', 7,
   'Live current position — the employment authority. Employment-verification '
   'tooling exists because this decays, so replaying a stale hit turns a '
   'verification into a recollection.'),

  ('clay', 'cpj-enrich-person', 7,
   'Indexed profile feeding the same "do they still work there" decision, so '
   'it gets the same window. Its payload carries last_refresh, reported '
   'alongside every weak verdict.'),

  ('blitz', '/v2/enrichment/email', 30,
   'Work email for the two-hop chain. Changes with employment, but it is an '
   'intermediate key rather than a verdict.'),

  ('blitz', '/v2/enrichment/email-to-person', 7,
   'Returns experiences[].job_is_current — employment, same window as the '
   'authority above.'),

  ('icypeas', '/api/find-people', 30,
   'Company ROSTER, used for joiner discovery and as an advisory hint only. It '
   'decides no verdict — a meaningful share of confirmed departures stay listed '
   'on it — so staleness costs a hint, not a conclusion. Per-lead credit cost '
   'makes a short window expensive for little gain.'),

  ('saleleads', '/api/v1/company/profile', 90,
   'Company size / industry / HQ. Moves far more slowly than a person''s job.'),

  ('apollo', '/v1/people/match', 30,
   'Person match including title and company: the same decay as employment, '
   'but consumed for sourcing rather than for verdicts.')
ON CONFLICT (api, endpoint) DO NOTHING;
