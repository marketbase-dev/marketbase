-- 051: register the `parallel_web_search` source type.
--
-- Buying-committee expansion is LinkedIn-shaped: it finds people Blitz can see
-- under a company's people search with a finance function facet. When a company
-- comes back with fewer than 5 committee members that is often CORRECT -- at a
-- 40-person firm the only senior finance person IS the seed. But it is
-- sometimes an indexing gap: the person exists and holds the title, and
-- LinkedIn simply does not tag them as finance.
--
-- This source type covers the second case: candidates found by asking the open
-- web (Parallel.ai search) for a company's finance leaders.
--
-- These rows are DELIBERATELY WEAKER EVIDENCE than every other source_type
-- here. The profile URL is a fact lifted from a search result, but the title
-- beside it was read off the page and may belong to a different person on the
-- same page. A row of this type is a lead to VERIFY, never a lead to push:
-- resolve the profile through the normal employment waterfall first.

INSERT INTO source_types (name, description, purpose, raw_data_shape, examples, created_by)
VALUES (
    'parallel_web_search',
    'Candidate finance contact at a target company, found by open-web search '
    '(Parallel.ai) rather than by LinkedIn people search.',
    'Use to fill the gap left when buying-committee expansion returns fewer '
    'members than the hand-over cap for a company, and the shortfall looks like '
    'a LinkedIn indexing miss rather than a genuinely small finance team. '
    'UNVERIFIED BY CONSTRUCTION: the linkedin_url comes from the search result, '
    'but title_seen_on_page is the nearest finance title in the surrounding '
    'text and is not guaranteed to be that person''s. Resolve the profile '
    '(saleleads /user/experience or Blitz) before qualifying or pushing. One '
    'row per (profile, claimed company) -- a fractional or portfolio CFO '
    'legitimately holds several companies at once, and each claim carries its '
    'own evidence.',
    'claimed_company, claimed_company_url, company_employees, '
    'committee_members_found, title_seen_on_page, excerpt, source_page, '
    'verified (bool).',
    'Under-5 committee gap fill, 2026-09-23: 116 companies searched, 157 unique '
    'candidate profiles, 13 survived employer verification. A ~8% yield -- the '
    'short committees were mostly real, not an indexing gap.',
    'migration-051'
)
ON CONFLICT (name) DO UPDATE SET
    description    = EXCLUDED.description,
    purpose        = EXCLUDED.purpose,
    raw_data_shape = EXCLUDED.raw_data_shape,
    examples       = EXCLUDED.examples;
