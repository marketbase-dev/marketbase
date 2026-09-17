-- 045_company_vendor_customers_view.sql
-- Retire the physical company_vendor_customers table, keep the NAME as a view.
--
-- Migration 038 created it as a vendor-specific workaround for a schema that
-- could only name one party to a relationship. company_edges + assertions now
-- hold the same facts for any pair of companies, so the table is redundant --
-- but readers exist, and the compatibility-view approach (§4 of the assertions
-- plan) is exactly how the other superseded tables are retired: keep the legacy
-- name, expose only ACTIVE rows, and callers stay correct without an audit.
--
-- Reading through active_assertions is the point: once an edge's assertion is
-- withdrawn (removed_at set), it leaves this view automatically, where the old
-- table required a DELETE that destroyed the history.

ALTER TABLE company_vendor_customers RENAME TO company_vendor_customers_legacy_038;

CREATE VIEW company_vendor_customers AS
SELECT
    a.id                       AS id,
    e.from_company_id          AS customer_company_id,
    e.to_company_id            AS vendor_company_id,
    CASE a.confidence
        WHEN 'suspected'   THEN 'probed'
        WHEN 'conflicting' THEN 'probed'
        ELSE 'confirmed'
    END                        AS certainty,
    COALESCE(
        s.payload->>'detection_method',
        CASE p.name WHEN 'jamf-saml-tenant-owner' THEN 'saml_tenant_owner'
                    WHEN 'jamf-tenant-dns'        THEN 'tenant_dns'
                    ELSE p.name END
    )                          AS detection_method,
    s.payload->>'evidence_url'  AS evidence_url,
    COALESCE(s.payload->>'evidence_note', a.notes) AS evidence_note,
    a.asserted_at              AS observed_at,
    a.asserted_by              AS source
FROM active_assertions a
JOIN company_edges e ON e.id = a.subject_company_edge_id
LEFT JOIN processors p ON p.id = a.processor_id
LEFT JOIN signals    s ON s.id = a.evidence_signal_id
WHERE a.namespace = 'relationship' AND a.key = 'customer';

COMMENT ON VIEW company_vendor_customers IS
    'Compatibility view over company_edges + active_assertions, replacing the '
    'migration-038 table of the same name. Shows only ACTIVE claims, so a '
    'withdrawn assertion disappears here without any history being destroyed. '
    'New code should read company_edges/active_assertions directly.';
