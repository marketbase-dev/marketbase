-- 098_in_touch.sql — who an operator is personally in touch with.
--
-- Automated outreach must not land on someone the operator already talks to.
-- "In touch" is read from what the instance holds, never stored: a view is
-- current state, and the tables under it stay the history.
--
-- A person is in touch with an operator when one of these holds:
--   whatsapp_direct     a direct chat where both sides wrote (094)
--   linkedin_messages   a LinkedIn conversation where both sides wrote
--   small_group         both are in one of the operator's messaging groups that
--                       has fewer members than in_touch_small_group_size()
--
-- A bigger shared group is not "in touch"; it stays a fact to mention, read
-- from v_collective_members.
--
-- v_lead_in_touch_reasons   one row per person, operator and reason
-- v_leads_in_touch          one row per person: the reasons and the last contact

-- The cutoff, counting the operator. Change it with one CREATE OR REPLACE.
CREATE OR REPLACE FUNCTION in_touch_small_group_size() RETURNS integer
LANGUAGE sql IMMUTABLE AS $$ SELECT 30 $$;

COMMENT ON FUNCTION in_touch_small_group_size() IS
    'A messaging group with fewer members than this (the operator included) makes its members personally in touch with the operator.';

CREATE OR REPLACE VIEW v_lead_in_touch_reasons AS
SELECT i.lead_id, 'whatsapp_direct'::text AS basis, c.operator_id, c.id AS ref_id,
       c.title AS detail, x.last_at
  FROM (SELECT chat_id, max(sent_at) AS last_at
          FROM operator_chat_messages
         WHERE NOT is_event
         GROUP BY chat_id
        HAVING bool_or(from_operator) AND bool_or(NOT from_operator)) x
  JOIN operator_chats c ON c.id = x.chat_id AND c.kind = 'direct'
  JOIN operator_chat_members m ON m.chat_id = c.id AND NOT m.is_self
  JOIN lead_identities i ON i.kind = 'whatsapp_lid' AND i.value = m.provider_member_id
UNION ALL
SELECT lc.lead_id, 'linkedin_messages', lc.operator_id, lc.id, NULL, x.last_at
  FROM (SELECT conversation_id, max(sent_at) AS last_at
          FROM lead_messages
         GROUP BY conversation_id
        HAVING bool_or(direction = 'inbound') AND bool_or(direction = 'outbound')) x
  JOIN lead_conversations lc ON lc.id = x.conversation_id AND lc.channel = 'linkedin'
UNION ALL
SELECT i.lead_id, 'small_group', oc.operator_id, g.id, g.name, NULL
  FROM (SELECT collective_id
          FROM collective_memberships
         WHERE member_state = 'member'
         GROUP BY collective_id
        HAVING count(*) + 1 < in_touch_small_group_size()) s
  JOIN collectives g ON g.id = s.collective_id AND g.kind = 'group'
  JOIN operator_chats oc ON oc.id = g.operator_chat_id
  JOIN collective_memberships m ON m.collective_id = g.id AND m.member_state = 'member'
  JOIN lead_identities i ON i.kind = m.member_kind AND i.value = m.member_value;

COMMENT ON VIEW v_lead_in_touch_reasons IS
    'Why a person counts as personally in touch with an operator: one row per person, operator and reason (a two-way direct chat, a two-way LinkedIn conversation, a shared small group). ref_id is the chat, conversation or group.';

CREATE OR REPLACE VIEW v_leads_in_touch AS
SELECT lead_id, array_agg(DISTINCT basis ORDER BY basis) AS reasons, max(last_at) AS last_contact_at
  FROM v_lead_in_touch_reasons
 GROUP BY lead_id;

COMMENT ON VIEW v_leads_in_touch IS
    'People an operator is personally in touch with, one row each. Automated outreach skips them (stage_to_campaign.py).';
