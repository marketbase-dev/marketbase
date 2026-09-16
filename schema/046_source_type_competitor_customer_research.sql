-- MarketBase schema, migration 046 — register the competitor_customer_research source type
--
-- Leads discovered by /find-confirmed-users: confirmed or assumed customers/users
-- of a competitor, found via web research (case studies, G2/Capterra reviews,
-- LinkedIn, job posts) and enriched with Blitz/TRIKit/Icypeas. Distinct from
-- find_competitor_salesperson (034), which targets the competitor's own reps —
-- this targets the competitor's CUSTOMERS, i.e. warm takeout prospects.
--
-- Idempotent. ingest_confirmed_users.py also ensures this row exists at write
-- time, so ingestion works even before this migration has propagated to every
-- instance via marketbase-migrate-all-clients. The company-to-company edge is
-- written to company_edges + assertions (read back via company_vendor_customers).

INSERT INTO source_types (name, description, purpose, raw_data_shape, examples, created_by) VALUES
  ('competitor_customer_research',
   'A confirmed or assumed customer/user of a competitor, found by /find-confirmed-users.',
   'Use for people discovered as users of a competitor product via web research (case studies, G2/Capterra reviews, LinkedIn, job posts) plus Blitz/TRIKit/Icypeas enrichment. raw_data carries the full flattened record: company, person, role, linkedin, email + status, the competitor they use, Confirmed/Assumed, and the evidence URL. The company-to-company edge is stored separately in company_edges + assertions.',
   'flattened record: customer_company(+website,+linkedin), customer_name, customer_role, linkedin_of_customer, customer_of_company, certainty(Confirmed|Assumed), email, email_status, evidence.',
   'find-confirmed-users (2026-09).',
   'migration-046')
ON CONFLICT (name) DO NOTHING;
