-- MarketBase schema, migration 038 — Competitor customers (who buys from whom)
--
-- `company_relationships` (007) is CLIENT-CENTRIC: it records what a company is
-- to US (competitor / customer / partner), one row per (company, relationship).
-- It has nowhere to express "org X is a customer OF competitor Y" — a
-- company-to-company edge. This migration adds that, plus the raw evidence
-- trail that produced it.
--
-- Two tables, deliberately separate:
--
--   company_vendor_customers — the CONCLUSION. A proven edge between two
--     companies. Only orgs we can actually attribute belong here.
--
--   vendor_tenant_probes — the EVIDENCE. One row per (vendor, method, domain)
--     probed, INCLUDING the negatives and the unattributable. Keyed by DOMAIN,
--     not by companies.id, precisely so that scanning thousands of orgs does
--     not require manufacturing thousands of company rows. Re-running a scan
--     is then cheap (skip what's already conclusive) and a later classifier
--     improvement can re-derive conclusions without re-scraping.

-- ── A. Proven vendor → customer edges ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS company_vendor_customers (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    vendor_company_id   uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,

    -- 'confirmed' — hard evidence (vendor case study, or a cryptographic-grade
    --               probe match such as an Entra tenant-GUID match).
    -- 'assumed'   — asserted somewhere but not independently verified.
    -- 'probed'    — inferred from a probe whose attribution is weaker.
    certainty        text,

    -- How we learned it: 'case_study' | 'sso_probe' | 'tenant_dns' | 'manual'.
    -- Part of the unique key ON PURPOSE: a case-study confirmation and an
    -- independent probe of the same pair should CORROBORATE each other as two
    -- rows, not silently overwrite one another.
    detection_method text NOT NULL DEFAULT 'manual',

    evidence_url     text,
    evidence_note    text,
    observed_at      timestamptz NOT NULL DEFAULT now(),
    source           text NOT NULL DEFAULT 'manual',

    CONSTRAINT uq_vendor_customer_method
        UNIQUE (customer_company_id, vendor_company_id, detection_method),
    CONSTRAINT ck_vendor_customer_not_self
        CHECK (customer_company_id <> vendor_company_id)
);

CREATE INDEX IF NOT EXISTS idx_cvc_customer ON company_vendor_customers(customer_company_id);
CREATE INDEX IF NOT EXISTS idx_cvc_vendor   ON company_vendor_customers(vendor_company_id);
CREATE INDEX IF NOT EXISTS idx_cvc_method   ON company_vendor_customers(detection_method);

-- ── B. Raw probe evidence, domain-keyed ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS vendor_tenant_probes (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_company_id   uuid REFERENCES companies(id) ON DELETE SET NULL,
    vendor_label        text NOT NULL,          -- 'jamf', 'automox', …
    method              text NOT NULL,          -- 'tenant_dns' | 'sso_probe'

    target_domain       text NOT NULL,          -- the key we actually trust
    target_company_name text,                   -- as supplied by the input list; may be WRONG

    tenant_label        text,                   -- the subdomain guess that was tested
    dns_cname           text,
    http_status         integer,
    final_url           text,
    attribution         text,                   -- own_domain | entra_match | entra_mismatch | unattributed | …
    verdict             text NOT NULL,          -- confirmed_customer | tenant_unattributed | tenant_other_owner | no_tenant
    evidence_path       text,
    raw                 jsonb,
    scanned_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_vendor_probe UNIQUE (vendor_label, method, target_domain)
);

CREATE INDEX IF NOT EXISTS idx_vtp_vendor  ON vendor_tenant_probes(vendor_label);
CREATE INDEX IF NOT EXISTS idx_vtp_verdict ON vendor_tenant_probes(verdict);
CREATE INDEX IF NOT EXISTS idx_vtp_domain  ON vendor_tenant_probes(target_domain);

-- ── C. Canonical read view ──────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_competitor_customers AS
SELECT
    cust.id            AS customer_company_id,
    cust.name          AS customer_name,
    cust.website       AS customer_website,
    cust.linkedin_url  AS customer_linkedin_url,
    vend.id            AS vendor_company_id,
    vend.name          AS vendor_name,
    cr.scope           AS vendor_scope,
    cvc.certainty,
    cvc.detection_method,
    cvc.evidence_url,
    cvc.evidence_note,
    cvc.observed_at
FROM company_vendor_customers cvc
JOIN companies cust ON cust.id = cvc.customer_company_id
JOIN companies vend ON vend.id = cvc.vendor_company_id
LEFT JOIN company_relationships cr
       ON cr.company_id = vend.id AND cr.relationship = 'competitor'
ORDER BY vend.name, cust.name;
