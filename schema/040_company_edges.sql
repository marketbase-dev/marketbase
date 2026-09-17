-- 040_company_edges.sql
-- Company <-> company relationships as a first-class relation.
--
-- 007_company_relationships can only name ONE party: the counterparty is always
-- implicitly the client, so it cannot express "acme is a customer of Jamf".
-- 038's company_vendor_customers was a vendor-specific workaround for exactly
-- that gap. This generalises both: any company, any counterparty, any type --
-- our customers, our partners' customers, a competitor's customers.
--
-- The edge row carries IDENTITY ONLY. Every judgement about it (certainty,
-- who decided, why, when it was withdrawn) is an assertion whose subject is the
-- edge -- see 042_assertions.sql. The edge is created by whoever first observes
-- it and is never deleted; liveness is the presence of an active assertion, so
-- presence-is-not-active lives in exactly one place.

CREATE TABLE IF NOT EXISTS company_edges (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Direction convention: the row reads "<from> <relationship> <to>".
    -- "acme is a customer of Jamf"  -> from=acme, to=jamf,  relationship='customer'
    -- "Jamf is a competitor of us"  -> from=jamf, to=<self>, relationship='competitor'
    from_company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    to_company_id   uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,

    -- customer | partner | vendor | competitor | investor | ...
    -- Registered in tag_definitions with layer='relationship'.
    relationship    text NOT NULL,

    first_seen_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT company_edges_not_self CHECK (from_company_id <> to_company_id),
    CONSTRAINT uq_company_edge UNIQUE (from_company_id, to_company_id, relationship)
);

CREATE INDEX IF NOT EXISTS idx_company_edges_to   ON company_edges (to_company_id, relationship);
CREATE INDEX IF NOT EXISTS idx_company_edges_from ON company_edges (from_company_id, relationship);

COMMENT ON TABLE company_edges IS
    'Directed company-to-company relationships, reading "<from> <relationship> <to>". '
    'Identity only: certainty, evidence and lifecycle are assertions whose subject '
    'is the edge. Never deleted -- liveness is an active assertion.';
