-- 098_small_group_ties.sql — the people who share a small messaging group with an operator.
--
-- Sharing a small group is one of the ways an operator is personally in touch
-- with someone, so automated outreach must not land on them. This is read from
-- what the instance holds, never stored: a view is current state, and the
-- membership tables under it stay the history. The tool that decides who is in
-- touch reads this view as one of its channels.
--
-- A bigger shared group is not "in touch"; it stays a fact to mention, read
-- from v_collective_members.

-- The cutoff, counting the operator. Change it with one CREATE OR REPLACE.
CREATE OR REPLACE FUNCTION in_touch_small_group_size() RETURNS integer
LANGUAGE sql IMMUTABLE AS $$ SELECT 30 $$;

COMMENT ON FUNCTION in_touch_small_group_size() IS
    'A messaging group with fewer members than this (the operator included) makes its members personally in touch with the operator.';

CREATE OR REPLACE VIEW v_lead_small_group_ties AS
SELECT i.lead_id, oc.operator_id, g.id AS collective_id, g.name AS group_name, s.members + 1 AS group_size
  FROM (SELECT collective_id, count(*) AS members
          FROM collective_memberships
         WHERE member_state = 'member'
         GROUP BY collective_id
        HAVING count(*) + 1 < in_touch_small_group_size()) s
  JOIN collectives g ON g.id = s.collective_id AND g.kind = 'group'
  JOIN operator_chats oc ON oc.id = g.operator_chat_id
  JOIN collective_memberships m ON m.collective_id = g.id AND m.member_state = 'member'
  JOIN lead_identities i ON i.kind = m.member_kind AND i.value = m.member_value;

COMMENT ON VIEW v_lead_small_group_ties IS
    'One row per person and small group they share with an operator: a current member of one of the operator''s messaging groups that has fewer than in_touch_small_group_size() members, the operator included.';
