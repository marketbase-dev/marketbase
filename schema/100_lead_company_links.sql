-- 100_lead_company_links.sql — a person who may open a door into a company.
--
-- lead_company_links   one row per person, company, link type and "via" company:
--     person     keyed by an IDENTITY (lead_identities kind + value), as 096 and 099
--                key people, so merging the person needs no change here
--     company    the company the person may be linked to
--     link_type  why they may be linked:
--                  portfolio_company  the company is in the portfolio of a fund the person
--                                     works at (via_company_id). It does NOT say the person made
--                                     or led the investment; nobody may have checked that.
--     certainty  possible   inferred from structure (a fund's portfolio page), not confirmed
--                confirmed  the person, or the operator, confirmed the tie
--     via        the company that carries the link (the fund), when there is one
-- Every row says who reported it (source). A person can be linked to many companies and a
-- company to many people, which is why this is a table and not an assertion (one active
-- assertion per subject and key would let the second person replace the first).
--
-- Rows are never deleted. A link found wrong is retired (retired_at + retired_reason), so
-- the record keeps what was believed and when.

CREATE TABLE IF NOT EXISTS lead_company_links (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_kind    text NOT NULL REFERENCES lead_identity_kinds(name),
    person_value   text NOT NULL,
    company_id     uuid NOT NULL REFERENCES companies(id),
    link_type      text NOT NULL CHECK (link_type IN ('portfolio_company')),
    certainty      text NOT NULL CHECK (certainty IN ('possible', 'confirmed')),
    via_company_id uuid REFERENCES companies(id),
    source         text NOT NULL,
    note           text,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    retired_at     timestamptz,
    retired_reason text,
    CONSTRAINT lead_company_links_via_not_self CHECK (via_company_id IS DISTINCT FROM company_id),
    CONSTRAINT lead_company_links_portfolio_has_via
        CHECK (link_type <> 'portfolio_company' OR via_company_id IS NOT NULL),
    CONSTRAINT lead_company_links_retired_pair CHECK ((retired_at IS NULL) = (retired_reason IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_company_links_active
    ON lead_company_links (person_kind, person_value, company_id, link_type, via_company_id)
    NULLS NOT DISTINCT WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_lead_company_links_company ON lead_company_links (company_id);
CREATE INDEX IF NOT EXISTS idx_lead_company_links_person ON lead_company_links (person_kind, person_value);

COMMENT ON TABLE lead_company_links IS
    'A person who may be linked to a company, by link type, with the company that carries the link; '
    'people keyed by identity. Never deleted: retired. Read through v_lead_company_links.';
COMMENT ON COLUMN lead_company_links.link_type IS
    'portfolio_company: the company is in the portfolio of the fund (via_company_id) the person works at. '
    'Not a claim that the person made the investment.';
COMMENT ON COLUMN lead_company_links.certainty IS
    'possible: inferred, not confirmed with anyone. confirmed: the person or the operator confirmed it.';
COMMENT ON COLUMN lead_company_links.source IS
    'Who reported the link, and where: a person and a date, a page.';

CREATE OR REPLACE VIEW v_lead_company_links AS
SELECT x.id, p.lead_id AS person_lead_id, l.name AS person_name,
       x.company_id, c.name AS company_name, x.link_type, x.certainty,
       x.via_company_id, v.name AS via_company, x.source, x.note, x.recorded_at,
       x.person_kind, x.person_value
  FROM lead_company_links x
  LEFT JOIN lead_identities p ON p.kind = x.person_kind AND p.value = x.person_value
  LEFT JOIN leads l ON l.id = p.lead_id
  JOIN companies c ON c.id = x.company_id
  LEFT JOIN companies v ON v.id = x.via_company_id
 WHERE x.retired_at IS NULL;

COMMENT ON VIEW v_lead_company_links IS
    'Active (not retired) lead_company_links, with the person and both companies named.';
