-- 056_rep_discovery_policy.sql
-- HOW a competitor's salespeople are discovered becomes a fact code can read,
-- instead of prose only a human can act on.
--
-- Not every competitor is a company you can sweep. Two shapes break the
-- assumption that `linkedin_url` names the organisation whose sellers you want:
--
--   * an ACQUIRED BRAND, where the brand has faded from profiles and most of
--     its reps now carry the acquirer's name;
--   * a PRODUCT LINE, which never had a LinkedIn page of its own, so the URL on
--     the row is its parent's and nobody ever listed the product as an employer.
--
-- In both cases a plain page sweep returns the parent's entire sales force, of
-- which only a sliver sells the thing being tracked.
--
-- One client already carried exactly these instructions, correct and specific,
-- in `company_relationships.notes` - which brand was a product line, whose page
-- the URL really was, which past employers to search, which product keywords
-- identify a real seller. Nothing read them. A routine `--all` build swept the
-- parent's page and ingested 1,824 of its sellers at large, tagging 868 as
-- connection targets; a human had stripped the same tags by hand the day
-- before, for the same reason. That is the tell that the rule was in the wrong
-- place: a mandate living in free text is violated by the first tool that does
-- not read free text, and then re-violated on the next run.
--
-- Same lesson as the `_source` vocabulary and the keyed-writes hook. A
-- convention is only enforced where it is MACHINE-READABLE at the point of
-- action.
--
-- `rep_discovery` says which method applies. Default 'page_sweep' is what every
-- existing row already does, so this migration changes no behaviour on its own;
-- the exceptional rows are set by a data write in the client database that
-- holds them, never here.
--
-- `discovery_config` carries what the acquired-brand method needs, mirroring
-- the six fields `find-acquired-company-salespersons` takes on its command line:
--
--   {"brand":            "<brand token>",
--    "parents":          ["<acquirer>", "<prior parent>"],
--    "product_keywords": ["<product>", "<category phrase>"],
--    "linkedin_slug":    null,            -- null => PRODUCT-LINE MODE
--    "past_employers":   ["<prior employer>", "<original startup>"],
--    "negative_terms":   ["<sibling product>"]}
--
-- Two fields are load-bearing rather than optional. `linkedin_slug: null` is
-- not a missing value: it means the brand never had its own company page, so
-- the name-search pass is SKIPPED entirely, because every hit it could return
-- would be a namesake - a short brand token matches unrelated firms, exchanges
-- and media companies, and each of them "still lists the brand as employer",
-- which is otherwise the highest-scoring signal there is. Affiliation must
-- instead be proven from the live profile. `negative_terms` name sibling
-- products that share the brand token but are a different product, and are
-- stripped from the text BEFORE positive keywords are looked for, so someone
-- who names both still passes on the real one.
--
-- Deliberately NOT overloaded onto `scope`. That column answers "how close a
-- competitor is this" (direct / adjacent / aspirational); this answers "how do
-- we find their reps". Conflating a competitive judgement with a discovery
-- method would make both unreadable.

ALTER TABLE company_relationships
    ADD COLUMN IF NOT EXISTS rep_discovery text NOT NULL DEFAULT 'page_sweep',
    ADD COLUMN IF NOT EXISTS discovery_config jsonb;

-- A typo'd method must fail at write time, not silently sweep. A CHECK rather
-- than an enum, so adding a method is a migration and not a type dance.
ALTER TABLE company_relationships
    DROP CONSTRAINT IF EXISTS company_relationships_rep_discovery_known;
ALTER TABLE company_relationships
    ADD CONSTRAINT company_relationships_rep_discovery_known
    CHECK (rep_discovery IN ('page_sweep', 'acquired_brand'));

-- The config is required by the acquired-brand method and meaningless without
-- it: such a row with no config would fall through to whatever the caller
-- passed on the command line, which is the situation this replaces.
ALTER TABLE company_relationships
    DROP CONSTRAINT IF EXISTS company_relationships_acquired_brand_has_config;
ALTER TABLE company_relationships
    ADD CONSTRAINT company_relationships_acquired_brand_has_config
    CHECK (rep_discovery <> 'acquired_brand'
           OR (discovery_config ? 'brand'
               AND discovery_config ? 'parents'
               AND discovery_config ? 'product_keywords'));

COMMENT ON COLUMN company_relationships.rep_discovery IS
    'How this competitor''s salespeople are discovered. page_sweep = the '
    'linkedin_url is the organisation itself, sweep it. acquired_brand = the '
    'URL points at a parent org, or the brand is a product line, so a sweep '
    'returns mostly out-of-scope people; use find-acquired-company-salespersons '
    'with discovery_config. Added after an --all build swept a parent company''s '
    'entire sales force because the rule lived only in notes (2026-10-08).';

COMMENT ON COLUMN company_relationships.discovery_config IS
    'Parameters the acquired-brand method needs: brand, parents[], '
    'product_keywords[], linkedin_slug (null = product-line mode, name search '
    'skipped because every hit would be a namesake), past_employers[], '
    'negative_terms[] (sibling products sharing the brand token). Mirrors the '
    'six fields find_acquired.py takes on the command line, so the parameters '
    'are stored research rather than per-run arguments someone must remember.';
