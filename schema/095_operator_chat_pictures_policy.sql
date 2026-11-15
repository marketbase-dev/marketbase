-- 095: how long an operator's group picture stays fresh (sync_operator_chats.py, stage pictures).
--
-- A group's picture comes from Unipile's attendee-picture route, called with the chat id
-- ("Use a chat id to get the picture of a group chat"). The image is cached raw in
-- enrichment_calls (base64 with its content type), keyed by the chat id, so a page that shows
-- group logos reads it from MarketBase and never from the provider. A group without a picture
-- is cached too, as {"picture": null}, so it is not asked again every day.
--
-- Group pictures change rarely, so 30 days.

INSERT INTO enrichment_source_policy (api, endpoint, fresh_days, rationale) VALUES
  ('unipile', '/api/v1/chat_attendees/{id}/picture', 30,
   'An operator''s group picture through the operator''s own Unipile account, cached per chat '
   '(the chat id in the key). Pictures change rarely, so 30 days.')
ON CONFLICT (api, endpoint) DO NOTHING;
