-- 092_sales_nav_search_and_network_claims.sql
-- A Sales Navigator people search run through an operator's own Unipile account
-- (POST /api/v1/linkedin/search), once per relationship degree, and the two claims
-- about reaching the people it finds.
--
-- 1. Unipile search had no enrichment_source_policy row, so its cached pages never
--    expired. Pages are cached one by one, keyed on the search URL and cursor. 7
--    days, the default for a new endpoint: network distance, titles and shared-
--    connection counts drift, and a week-old page is fine to replay within a run
--    and its retries, but not as next month's answer.
--
-- 2. Three claims for lib/assertions.py, which refuses a (namespace, key) that is
--    not registered here:
--      network/mutual_connections   observation: the operator's own 1st-degree
--                                   connections who are connected to the person
--                                   (a Sales Nav connection-of search; verified)
--      network/intro_via_colleague  observation: the operator's own 1st-degree
--                                   connections who work, or worked, with the person
--                                   (shared employment; inferred)
--      network/introducer           decision: who will introduce us, as arranged

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('unipile', '/api/v1/linkedin/search', 7,
   'Sales Navigator / LinkedIn people search through the operator''s own Unipile account, '
   'cached per page (search URL and cursor in the key). Network distance, titles and '
   'shared-connection counts drift, so 7 days: the default for a new endpoint.')
ON CONFLICT (api, endpoint) DO NOTHING;

INSERT INTO tag_definitions (namespace, key, entity_type, layer, kind, description) VALUES
  ('network', 'mutual_connections', 'lead', 'attribute', 'observation',
   'The operator''s own 1st-degree connections who are connected to this person, as a Sales '
   'Navigator search (RELATIONSHIP 1st degree + CONNECTION_OF this person) returned them through '
   'the operator''s account: the people who can introduce. value = how many; notes = who, with '
   'title and company. Evidence: the cached search page (enrichment_calls), which holds the full '
   'list with member ids.'),
  ('network', 'intro_via_colleague', 'lead', 'attribute', 'observation',
   'A warm-intro path into this person through the operator''s own network: 1st-degree '
   'connections of the operator who work at the same company as this person now, or worked '
   'there at the same time, matched on the LinkedIn company id in both work histories (never '
   'on a company name). value = current_colleague | former_colleague, the strongest path; '
   'notes = every connector, strongest first, with the company and the overlap. Evidence: the '
   'strongest connector''s profile signal. Shared employment, not a verified LinkedIn '
   'connection between the two.'),
  ('network', 'introducer', 'lead', 'attribute', 'decision',
   'Who will introduce us to this person, as arranged with the introducer. value = the '
   'introducer''s lead id; reason = what the introducer agreed to and when; decided_by = who '
   'arranged it. A changed arrangement supersedes the active row.')
ON CONFLICT (namespace, key, entity_type) DO NOTHING;
