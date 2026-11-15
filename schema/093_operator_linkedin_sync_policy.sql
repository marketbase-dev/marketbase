-- 093_operator_linkedin_sync_policy.sql
-- tools/sync_operator_linkedin.py pulls an operator's own LinkedIn relationships
-- through their Unipile account: every chat and its messages, the reactions and
-- comments other people left on the operator's posts, and the reactions and
-- comments the operator left on other people's posts. Messages land in
-- lead_conversations / lead_messages; reactions and comments land in lead_touches
-- (kind 'linkedin', direction inbound or outbound, disposition 'reaction' or
-- 'comment'). Only people already in `leads` are written; everyone else stays in
-- the cached pages, so they attach on a later run once they are leads.
--
-- None of the eight endpoints had an enrichment_source_policy row, so their cached
-- pages never expired. The endpoint names are the tool's cache keys: a path with
-- an id in it is stored without the id, which sits in params instead.
--
-- 7 days is the default for a new endpoint. Two differ, on purpose:
--   * /api/v1/chats is the listing a sync starts from; a day-old page hides
--     yesterday's new chats, so 1 day.
--   * /api/v1/posts is one post looked up by id to learn who wrote it. The author
--     of a post does not change, so 365 days.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('unipile', '/api/v1/chats', 1,
   'An operator''s chat list, cached per page. It is the starting point of a message '
   'sync, and a new chat appears on page one, so 1 day.'),
  ('unipile', '/api/v1/chats/messages', 7,
   'One chat''s messages, cached per page (chat id in params). The sync re-reads a chat '
   'whose listing timestamp is newer than its last sync, so 7 days only bounds a replay.'),
  ('unipile', '/api/v1/users/posts', 7,
   'An operator''s own posts, cached per page (member id in params). New posts and '
   'changing counters, so 7 days: the default for a new endpoint.'),
  ('unipile', '/api/v1/posts/reactions', 7,
   'Reactions on one post, cached per page (post id in params). Reactions keep arriving '
   'for a few weeks, so 7 days: the default for a new endpoint.'),
  ('unipile', '/api/v1/posts/comments', 7,
   'Comments on one post, cached per page (post id in params). 7 days: the default for '
   'a new endpoint.'),
  ('unipile', '/api/v1/users/reactions', 7,
   'Reactions the operator left on other people''s posts, cached per page. 7 days: the '
   'default for a new endpoint.'),
  ('unipile', '/api/v1/users/comments', 7,
   'Comments the operator left on other people''s posts, cached per page. 7 days: the '
   'default for a new endpoint.'),
  ('unipile', '/api/v1/posts', 365,
   'One post looked up by id, to learn its author. A post''s author does not change, '
   'so 365 days.')
ON CONFLICT (api, endpoint) DO NOTHING;
