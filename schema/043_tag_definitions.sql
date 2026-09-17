-- 043_tag_definitions.sql
-- The registry. No such table exists in any client today: tag meanings live in
-- people's heads and chat transcripts. Relationship types are registered here
-- too (layer='relationship'), which is why no separate relationship-type
-- registry is needed.

CREATE TABLE IF NOT EXISTS tag_definitions (
    namespace     text NOT NULL,
    key           text NOT NULL,
    entity_type   text NOT NULL,   -- lead | company | company_edge | post_engagement | conversation
    layer         text NOT NULL CHECK (layer IN ('attribute','audience','state','relationship')),
    description   text NOT NULL,
    deprecated_by text,
    PRIMARY KEY (namespace, key, entity_type)
);

INSERT INTO tag_definitions (namespace, key, entity_type, layer, description) VALUES
  ('relationship', 'customer',   'company_edge', 'relationship',
   'The from-company buys/uses the to-company''s product. confidence=suspected for a single weak signal; NULL once corroborated.'),
  ('relationship', 'competitor', 'company_edge', 'relationship',
   'The from-company competes with the to-company. value carries direct|adjacent|aspirational.'),
  ('relationship', 'partner',    'company_edge', 'relationship',
   'Commercial partnership. value carries strategic|reseller|integration.'),
  ('relationship', 'vendor',     'company_edge', 'relationship',
   'The from-company sells to the to-company.')
ON CONFLICT (namespace, key, entity_type) DO NOTHING;

COMMENT ON TABLE tag_definitions IS
    'What every namespace/key means, per entity type. Register a tag here before '
    'writing it, so meanings stop living in chat transcripts.';
