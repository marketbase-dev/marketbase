-- 068_vendor_pricing_and_usage.sql
-- What each data vendor charges, and how much of it we use.
--
-- Before this, plan prices lived in a memory file and per-endpoint credit
-- costs in code comments, so "can we drop one of the RapidAPI scrapers?" took
-- a browser session to re-read four pricing pages and a crawl across six
-- instances to count calls. The numbers belong next to the call log they
-- price.
--
--   vendor_plans           one row per (api, plan, observed_on). A price change
--                          is a NEW row, never an UPDATE, so the history of what
--                          we paid stays readable. is_current marks our plan.
--   vendor_endpoint_costs  units one call costs, keyed on the SAME (api,
--                          endpoint) strings enrichment_calls logs, so the two
--                          join. Endpoints we have never called are listed too
--                          (by the vendor's own name), so capability questions
--                          are answerable from the table.
--   vendor_usage_snapshots calls per endpoint per instance over a window,
--                          written by vendor_usage_snapshot.py. Only Impact 11's
--                          own instance holds rows: the tool reads every
--                          instance and writes the house copy.
--
-- `api` is the label enrichment_calls uses. Fresh is logged under two labels
-- ('fresh' and 'fresh-linkedin-profile-data'); the plan row lists the second
-- in api_aliases and the snapshot tool folds it into the first.

CREATE TABLE IF NOT EXISTS vendor_plans (
    api                  text NOT NULL,
    plan                 text NOT NULL,
    observed_on          date NOT NULL,
    vendor               text NOT NULL,
    api_aliases          text[] NOT NULL DEFAULT '{}',
    is_current           boolean NOT NULL DEFAULT false,
    monthly_usd          numeric,
    included_units       integer,
    unit                 text NOT NULL,
    overage_usd_per_unit numeric,
    hard_request_cap     integer,
    rate_limit           text,
    source_url           text,
    notes                text,
    PRIMARY KEY (api, plan, observed_on)
);

COMMENT ON COLUMN vendor_plans.included_units IS
    'Units included per month. NULL = unlimited.';
COMMENT ON COLUMN vendor_plans.overage_usd_per_unit IS
    'Price per unit past included_units. NULL = hard limit, calls stop.';
COMMENT ON COLUMN vendor_plans.unit IS
    'What included_units and endpoint costs count: request, credit, usd, record.';

CREATE TABLE IF NOT EXISTS vendor_endpoint_costs (
    api            text NOT NULL,
    endpoint       text NOT NULL,
    observed_on    date NOT NULL,
    label          text NOT NULL,
    task           text NOT NULL,
    units_per_call numeric,
    basis          text NOT NULL DEFAULT 'per call',
    notes          text,
    source_url     text,
    PRIMARY KEY (api, endpoint, observed_on),
    CONSTRAINT vendor_endpoint_costs_task CHECK (task IN (
        'profile_read', 'member_id_to_link', 'company_employees',
        'company_lookup', 'post_engagement', 'post_search', 'person_posts',
        'company_posts', 'jobs', 'groups', 'ads', 'email', 'other'))
);

COMMENT ON COLUMN vendor_endpoint_costs.units_per_call IS
    'Units of vendor_plans.unit one call costs. 0 = free. NULL = unknown.';
COMMENT ON COLUMN vendor_endpoint_costs.basis IS
    'What one unit buys: per call, per page, per result, per search.';

CREATE TABLE IF NOT EXISTS vendor_usage_snapshots (
    snapshot_on   date NOT NULL,
    window_days   integer NOT NULL,
    instance      text NOT NULL,
    api           text NOT NULL,
    endpoint      text NOT NULL,
    task          text,
    calls         integer NOT NULL,
    successes     integer NOT NULL,
    est_units     numeric,
    computed_by   text NOT NULL,
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_on, window_days, instance, api, endpoint)
);

COMMENT ON TABLE vendor_usage_snapshots IS
    'Paid-vendor calls per instance over window_days ending snapshot_on. One '
    'enrichment_calls row is one real call (cache hits write nothing). '
    'est_units = calls x units_per_call, a FLOOR for per-page/per-result '
    'endpoints. Written only by vendor_usage_snapshot.py.';

CREATE OR REPLACE VIEW vendor_current_plans AS
SELECT DISTINCT ON (api) *
  FROM vendor_plans
 WHERE is_current
 ORDER BY api, observed_on DESC;

-- ── plans (RapidAPI pages read from the logged-in account, 2026-10-30) ─────────
INSERT INTO vendor_plans (api, plan, observed_on, vendor, api_aliases, is_current,
    monthly_usd, included_units, unit, overage_usd_per_unit, hard_request_cap,
    rate_limit, source_url, notes) VALUES
  ('saleleads', 'Basic', '2026-10-30', 'Fresh LinkedIn Scraper API (saleleads.ai, RapidAPI)', '{}', false,
   0, 50, 'request', NULL, NULL, '1000/hour',
   'https://rapidapi.com/saleleadsdotai-saleleadsdotai-default/api/fresh-linkedin-scraper-api/pricing', NULL),
  ('saleleads', 'Pro', '2026-10-30', 'Fresh LinkedIn Scraper API (saleleads.ai, RapidAPI)', '{}', true,
   49, 20000, 'request', 0.008, NULL, '20/minute',
   'https://rapidapi.com/saleleadsdotai-saleleadsdotai-default/api/fresh-linkedin-scraper-api/pricing',
   'Lapsed 2026-10-29, renewed the same day. Each include_* option on a profile call adds 1 request.'),
  ('saleleads', 'Ultra', '2026-10-30', 'Fresh LinkedIn Scraper API (saleleads.ai, RapidAPI)', '{}', false,
   249, 150000, 'request', 0.0065, NULL, '120/minute',
   'https://rapidapi.com/saleleadsdotai-saleleadsdotai-default/api/fresh-linkedin-scraper-api/pricing', NULL),
  ('saleleads', 'Mega', '2026-10-30', 'Fresh LinkedIn Scraper API (saleleads.ai, RapidAPI)', '{}', false,
   499, 350000, 'request', 0.005, NULL, '300/minute',
   'https://rapidapi.com/saleleadsdotai-saleleadsdotai-default/api/fresh-linkedin-scraper-api/pricing', NULL),

  ('fresh', 'Basic', '2026-10-30', 'Fresh LinkedIn Profile Data (FreshData, RapidAPI)', '{fresh-linkedin-profile-data}', false,
   10, 500, 'credit', NULL, 500, '20/minute',
   'https://rapidapi.com/freshdata-freshdata-default/api/fresh-linkedin-profile-data/pricing', NULL),
  ('fresh', 'Pro', '2026-10-30', 'Fresh LinkedIn Profile Data (FreshData, RapidAPI)', '{fresh-linkedin-profile-data}', true,
   45, 4500, 'credit', 0.012, 6000, '20/minute',
   'https://rapidapi.com/freshdata-freshdata-default/api/fresh-linkedin-profile-data/pricing',
   'Lapsed 2026-10-29, renewed the same day. Credits overage-bill; REQUESTS hard-stop at 6,000.'),
  ('fresh', 'Ultra', '2026-10-30', 'Fresh LinkedIn Profile Data (FreshData, RapidAPI)', '{fresh-linkedin-profile-data}', false,
   250, 32000, 'credit', 0.008, 40000, '50/minute',
   'https://rapidapi.com/freshdata-freshdata-default/api/fresh-linkedin-profile-data/pricing', NULL),
  ('fresh', 'Mega', '2026-10-30', 'Fresh LinkedIn Profile Data (FreshData, RapidAPI)', '{fresh-linkedin-profile-data}', false,
   1000, 200000, 'credit', 0.006, 240000, '150/minute',
   'https://rapidapi.com/freshdata-freshdata-default/api/fresh-linkedin-profile-data/pricing', NULL),

  ('realtime-linkedin', 'Basic', '2026-10-30', 'Real-Time LinkedIn Scraper API (rockapis, RapidAPI)', '{}', true,
   0, 50, 'credit', NULL, 75, '1000/hour',
   'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api/pricing',
   '2026-10-30: every call to /get-profile-data-by-url answered "This version of the service is no '
   'longer available" and pointed to support@professionalnetworkdata.com. Treat as not working.'),
  ('realtime-linkedin', 'Pro', '2026-10-30', 'Real-Time LinkedIn Scraper API (rockapis, RapidAPI)', '{}', false,
   175, 50000, 'credit', 0.004, 60000, '120/minute',
   'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api/pricing', NULL),
  ('realtime-linkedin', 'Ultra', '2026-10-30', 'Real-Time LinkedIn Scraper API (rockapis, RapidAPI)', '{}', false,
   300, 100000, 'credit', 0.0035, 120000, '240/minute',
   'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api/pricing', NULL),
  ('realtime-linkedin', 'Mega', '2026-10-30', 'Real-Time LinkedIn Scraper API (rockapis, RapidAPI)', '{}', false,
   500, 200000, 'credit', 0.0025, 250000, '320/minute',
   'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api/pricing', NULL),

  ('blitz', 'Shared seat', '2026-10-30', 'Blitz API', '{}', true,
   NULL, NULL, 'record', 0, NULL, '50/second',
   'https://api.blitz-api.ai',
   'BLITZAPI_API_KEY_SHARED: unmetered house seat registered to a client, agreed as shared '
   'capacity. Zero marginal cost. Every other BLITZAPI_API_KEY_* is metered and person/customer scoped.'),
  ('icypeas', 'Current', '2026-10-30', 'Icypeas', '{}', true,
   NULL, NULL, 'credit', NULL, NULL, NULL,
   'https://api-doc.icypeas.com/how-works/credit-cost/',
   'Plan price not recorded. No quota (quotas.daily in subscription_info is NOT enforced); credits are the only limit.'),
  ('deepline', 'Pay as you go', '2026-10-30', 'Deepline', '{}', true,
   NULL, NULL, 'usd', NULL, NULL, NULL, NULL,
   '1 Deepline credit = $0.10. Endpoint costs below are in dollars.'),
  ('clay', 'Workspace credits', '2026-10-30', 'Clay', '{}', true,
   NULL, NULL, 'credit', NULL, NULL, NULL, NULL,
   'Billed to the client''s Clay workspace. Action test-runs capped ~25/day (observed); routines uncapped.')
ON CONFLICT (api, plan, observed_on) DO NOTHING;

-- ── endpoint costs ───────────────────────────────────────────────────────────
INSERT INTO vendor_endpoint_costs (api, endpoint, observed_on, label, task,
    units_per_call, basis, notes, source_url) VALUES
  -- saleleads: 1 request per call unless noted. Logged endpoint labels vary by caller.
  ('saleleads', 'user/experience',          '2026-10-30', 'Get User Experiences', 'profile_read', 1, 'per call', 'Current job; no person location (job location null ~84%).', 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', '/user/experience',         '2026-10-30', 'Get User Experiences', 'profile_read', 1, 'per call', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', '/user/profile',            '2026-10-30', 'Get User Profile', 'profile_read', 1, 'per call', 'Returns member ID + person country. +1 per include_* option. Cannot turn a member ID into a link.', 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', '/api/v1/user/profile',     '2026-10-30', 'Get User Profile', 'profile_read', 1, 'per call', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', '/list-people',             '2026-10-30', 'Search People', 'company_employees', 1, 'per page', 'Keyword search; headline-only title match, no company id on rows. Top-up source (~11% unique vs Icypeas+Blitz).', 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', 'list-people',              '2026-10-30', 'Search People', 'company_employees', 1, 'per page', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', 'cli:list-people',          '2026-10-30', 'Search People', 'company_employees', 1, 'per page', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', 'Get Company People',       '2026-10-30', 'Get Company People', 'company_employees', 1, 'per page', 'Never called; untested.', 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', '/api/v1/company/profile',  '2026-10-30', 'Get Company Profile', 'company_lookup', 1, 'per call', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', 'company-profile',          '2026-10-30', 'Get Company Profile', 'company_lookup', 1, 'per call', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', 'cli:company-profile',      '2026-10-30', 'Get Company Profile', 'company_lookup', 1, 'per call', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', '/api/v1/post/comments',    '2026-10-30', 'Get Post Comments', 'post_engagement', 1, 'per page', '10 comments per page.', 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', 'Get Post Reactions',       '2026-10-30', 'Get Post Reactions', 'post_engagement', 1, 'per page', 'Page size not measured.', 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', '/api/v1/search/posts',     '2026-10-30', 'Search Posts', 'post_search', 1, 'per page', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', '/api/v1/user/posts',       '2026-10-30', 'Get User Posts', 'person_posts', 1, 'per page', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', 'Get Company Posts',        '2026-10-30', 'Get Company Posts', 'company_posts', 1, 'per page', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', 'Search Jobs',              '2026-10-30', 'Search Jobs / Get Job Detail / Get Company Jobs', 'jobs', 1, 'per call', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', 'Get Group Info',           '2026-10-30', 'Get Group Info / Get Group Posts', 'groups', 1, 'per call', NULL, 'https://docs.saleleads.ai/llms.txt'),
  ('saleleads', 'Search Ads',               '2026-10-30', 'Search Ads / Get Ad Detail', 'ads', 1, 'per call', NULL, 'https://docs.saleleads.ai/llms.txt'),

  -- Fresh: credits. https://fdocs.info/api-reference/understanding-credit-usage.md
  ('fresh', '/enrich-lead',          '2026-10-30', 'Get Personal Profile', 'profile_read', 2, 'per call', '+1 credit per extra include_* option. Takes a member ID: the member-ID-to-link path. Its `country` field is the COMPANY HQ; the person''s is in the location string.', 'https://fdocs.info/api-reference/understanding-credit-usage.md'),
  ('fresh', '/search-employees',     '2026-10-30', 'Search Employees', 'company_employees', 10, 'per search', '+1 credit per profile returned; status checks free.', 'https://fdocs.info/api-reference/understanding-credit-usage.md'),
  ('fresh', '/get-post-reactions',   '2026-10-30', 'Get Post Reactions', 'post_engagement', 1, 'per page', '50 reactions per page.', 'https://fdocs.info/api-reference/understanding-credit-usage.md'),
  ('fresh', '/get-post-comments',    '2026-10-30', 'Get Post Comments', 'post_engagement', 1, 'per page', '~3-10 comments per page observed.', 'https://fdocs.info/api-reference/understanding-credit-usage.md'),
  ('fresh', '/search-posts',         '2026-10-30', 'Search Posts', 'post_search', 1, 'per 20 posts', 'Max ~100 posts per search.', 'https://fdocs.info/api-reference/understanding-credit-usage.md'),
  ('fresh', '/get-profile-posts',    '2026-10-30', 'Get Profile Posts', 'person_posts', 2, 'per call', NULL, 'https://fdocs.info/api-reference/understanding-credit-usage.md'),
  ('fresh', '/get-company-posts',    '2026-10-30', 'Get Company Posts', 'company_posts', 2, 'per call', NULL, 'https://fdocs.info/api-reference/understanding-credit-usage.md'),
  ('fresh', '/get-company-by-linkedinurl', '2026-10-30', 'Company by URL / domain / ID', 'company_lookup', 1, 'per call', NULL, 'https://fdocs.info/api-reference/understanding-credit-usage.md'),
  ('fresh', '/search-jobs',          '2026-10-30', 'Search Jobs', 'jobs', 2, 'per call', 'Job details: 1 credit. No groups or ads endpoints.', 'https://fdocs.info/api-reference/understanding-credit-usage.md'),

  -- Real-Time (rockapis): credits, from the API overview's "Credit Usage by Endpoints". Not working 2026-10-30.
  ('realtime-linkedin', '/get-profile-data-by-url', '2026-10-30', 'Get Profile Data By URL', 'profile_read', 1, 'per call', 'Returned "service no longer available" on 6/6 calls, 2026-10-30.', 'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api'),
  ('realtime-linkedin', 'Search People',            '2026-10-30', 'Search People', 'company_employees', 1, 'per call', 'Untested.', 'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api'),
  ('realtime-linkedin', 'Get Company Details',      '2026-10-30', 'Get Company Details / By Domain / Search Companies', 'company_lookup', 1, 'per call', 'Company Insights [PREMIUM]: 5 credits.', 'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api'),
  ('realtime-linkedin', 'Get Post Reactions',       '2026-10-30', 'Get Post Reactions / Profile Post Comments', 'post_engagement', 1, 'per call', NULL, 'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api'),
  ('realtime-linkedin', 'Search Posts',             '2026-10-30', 'Search Posts / by Hashtag', 'post_search', 1, 'per call', NULL, 'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api'),
  ('realtime-linkedin', 'Get Profile''s Posts',     '2026-10-30', 'Get Profile''s Posts', 'person_posts', 1, 'per call', NULL, 'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api'),
  ('realtime-linkedin', 'Get Company''s Post',      '2026-10-30', 'Get Company''s Post', 'company_posts', 1, 'per call', NULL, 'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api'),
  ('realtime-linkedin', 'Search Jobs',              '2026-10-30', 'Search Jobs / Job Details / Hiring Team / Company Jobs', 'jobs', 1, 'per call', 'No groups or ads endpoints (only "group interests" of a profile).', 'https://rapidapi.com/rockapis-rockapis-default/api/linkedin-data-api'),

  -- Blitz: shared seat, zero marginal cost.
  ('blitz', '/v2/enrichment/person',            '2026-10-30', 'Person enrichment', 'profile_read', 0, 'per call', 'Takes a member-ID URL and returns the current link (10/12) and country_code (12/16), 16-person test 2026-10-29. Returns NO member ID. Index ~one job behind: never an employment verdict.', NULL),
  ('blitz', '/v2/enrichment/company',           '2026-10-30', 'Company enrichment', 'company_lookup', 0, 'per call', NULL, NULL),
  ('blitz', '/v2/enrichment/domain-to-linkedin','2026-10-30', 'Domain to LinkedIn', 'company_lookup', 0, 'per call', NULL, NULL),
  ('blitz', '/v2/search/people',                '2026-10-30', 'People search', 'company_employees', 0, 'per page', 'Only company.name / company.linkedin_url / job_title filters work.', NULL),
  ('blitz', 'search-people',                    '2026-10-30', 'People search', 'company_employees', 0, 'per page', NULL, NULL),
  ('blitz', '/v2/jobs/company',                 '2026-10-30', 'Company jobs', 'jobs', 0, 'per call', 'No posts, reactions, groups or ads endpoints.', NULL),
  ('blitz', '/enrichment/email',                '2026-10-30', 'Work email', 'email', 0, 'per call', NULL, NULL),
  ('blitz', '/enrichment/email-to-person',      '2026-10-30', 'Email to person', 'profile_read', 0, 'per call', NULL, NULL),

  -- Icypeas: credits.
  ('icypeas', '/api/find-people',          '2026-10-30', 'Find people', 'company_employees', 0.02, 'per result', 'Primary roster source.', 'https://api-doc.icypeas.com/how-works/credit-cost/'),
  ('icypeas', '/api/find-people/count',    '2026-10-30', 'Find people count', 'company_employees', 0, 'per call', NULL, 'https://api-doc.icypeas.com/how-works/credit-cost/'),
  ('icypeas', '/api/url-search/profile',   '2026-10-30', 'Profile URL search', 'profile_read', 1, 'per call', 'Profile scrape 1.5, company scrape 0.5, email 1, reverse email 10. No posts, jobs, groups or ads.', 'https://api-doc.icypeas.com/how-works/credit-cost/'),

  -- Deepline: dollars.
  ('deepline', 'harvestapi_get_profile',                         '2026-10-30', 'harvestapi profile', 'member_id_to_link', 0.005, 'per call', 'Param `url` (NOT linkedin_url). Member ID to link 12/12, country 16/16, both ID forms returned. ok:false passes api_cache.is_success: pass a success_fn.', NULL),
  ('deepline', 'edges_linkedin_extract_people_experiences',     '2026-10-30', 'edges experiences', 'profile_read', 0.003, 'per call', 'Approximate (56 calls ~ $0.17).', NULL),
  ('deepline', 'edges_linkedin_extract_linkedin_profile_handle', '2026-10-30', 'edges profile handle', 'member_id_to_link', 0.042, 'per call', NULL, NULL),
  ('deepline', 'harvestapi_post_reactions',                      '2026-10-30', 'harvestapi post reactions / comments', 'post_engagement', 0.003, 'per page', 'edges likers $0.002/result, commenters $0.005/result.', NULL),
  ('deepline', 'harvestapi_search_posts',                        '2026-10-30', 'harvestapi post search', 'post_search', 0.003, 'per page', NULL, NULL),
  ('deepline', 'harvestapi_profile_posts',                       '2026-10-30', 'harvestapi profile posts', 'person_posts', 0.003, 'per page', NULL, NULL),
  ('deepline', 'harvestapi_company_posts',                       '2026-10-30', 'harvestapi company posts', 'company_posts', 0.003, 'per page', NULL, NULL),
  ('deepline', 'harvestapi_jobs_search',                         '2026-10-30', 'harvestapi jobs', 'jobs', 0.001, 'per page', NULL, NULL),
  ('deepline', 'harvestapi_groups_search',                       '2026-10-30', 'harvestapi groups', 'groups', 0.002, 'per page', NULL, NULL),
  ('deepline', 'harvestapi_ads_search',                          '2026-10-30', 'harvestapi ads', 'ads', 0.002, 'per page', NULL, NULL),
  ('deepline', 'edges_employee_search',                          '2026-10-30', 'edges employee search', 'company_employees', 0.042, 'per result', NULL, NULL),
  ('deepline', 'contextdev_get_web_competitors',                 '2026-10-30', 'web competitors', 'other', 0, 'per call', NULL, NULL),

  -- Clay: workspace credits.
  ('clay', 'cpj-enrich-person',      '2026-10-30', 'Enrich Person action', 'profile_read', 1, 'per call', NULL, NULL),
  ('clay', 'routine:enrich-person',  '2026-10-30', 'Enrich Person routine', 'profile_read', 1, 'per call', NULL, NULL)
ON CONFLICT (api, endpoint, observed_on) DO NOTHING;
