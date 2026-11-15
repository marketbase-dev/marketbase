-- 093_operator_chats.sql
--
-- An operator's own chats on a messaging channel (WhatsApp first), who is in
-- them, and their messages, synced daily from the operator's Unipile account.
--
-- lead_conversations (023) holds an operator's thread with ONE known lead. A
-- messaging account is wider than that: most chats are with people who are not
-- leads, and a group has many people. So these tables are keyed by the chat and
-- by the network's own member ids, and link to people through lead_identities
-- (kind 'whatsapp', the number) rather than a lead_id column. That also keeps
-- merge_lead() unchanged: it moves lead ids by table name, and nothing here
-- holds one.
--
--   operator_sync_policy      per operator: what is stored. 1:1 messages yes or
--                             no; group messages only for monitored groups (or
--                             every group); how far back to backfill.
--   operator_chats            one row per chat the account can see.
--   operator_chat_members     who is in each chat, for EVERY group, monitored or
--                             not. A member who is no longer listed gets
--                             left_at; nobody is deleted.
--   operator_chat_messages    the messages the policy says to store, with the
--                             provider's raw payload.
--   operator_chat_monitoring  append-only decisions: monitor this group or stop.
--                             The latest decision per chat is current
--                             (v_operator_chat_monitoring); earlier ones stay.

CREATE TABLE IF NOT EXISTS operator_sync_policy (
    operator_id              uuid PRIMARY KEY REFERENCES outbound_operators(id),
    store_direct_messages    boolean NOT NULL,
    store_group_messages     text NOT NULL CHECK (store_group_messages IN ('monitored_only', 'all', 'none')),
    backfill                 text NOT NULL CHECK (backfill IN ('all_history', 'from_first_sync')),
    decided_by               text NOT NULL,
    decided_at               timestamptz NOT NULL DEFAULT now(),
    note                     text
);

CREATE TABLE IF NOT EXISTS operator_chats (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id          uuid NOT NULL REFERENCES outbound_operators(id),
    provider_chat_id     text NOT NULL,                 -- Unipile chat id
    provider_thread_id   text,                          -- the network's own id
    kind                 text NOT NULL CHECK (kind IN ('direct', 'group')),
    title                text,
    first_seen_at        timestamptz NOT NULL DEFAULT now(),
    last_activity_at     timestamptz,
    archived             boolean,
    read_only            boolean,
    members_synced_at    timestamptz,
    messages_synced_at   timestamptz,
    oldest_message_at    timestamptz,
    history_complete     boolean NOT NULL DEFAULT false, -- the backfill reached the provider's oldest message
    raw                  jsonb,
    CONSTRAINT uq_operator_chats UNIQUE (operator_id, provider_chat_id)
);

CREATE TABLE IF NOT EXISTS operator_chat_members (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id              uuid NOT NULL REFERENCES operator_chats(id),
    provider_member_id   text NOT NULL,                 -- the network's member id
    phone                text,
    display_name         text,
    is_self              boolean NOT NULL DEFAULT false,
    is_admin             boolean,                       -- NULL: the provider does not say
    first_seen_at        timestamptz NOT NULL DEFAULT now(),
    last_seen_at         timestamptz NOT NULL DEFAULT now(),
    left_at              timestamptz,                   -- no longer listed on a later sync
    raw                  jsonb,
    CONSTRAINT uq_operator_chat_members UNIQUE (chat_id, provider_member_id)
);
CREATE INDEX IF NOT EXISTS idx_operator_chat_members_phone ON operator_chat_members (phone);

CREATE TABLE IF NOT EXISTS operator_chat_messages (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id              uuid NOT NULL REFERENCES operator_chats(id),
    provider_message_id  text NOT NULL,
    sender_member_id     text,                          -- provider member id of the sender
    from_operator        boolean NOT NULL,
    sent_at              timestamptz NOT NULL,
    body                 text,
    is_event             boolean NOT NULL DEFAULT false, -- joins, reactions, renames
    raw                  jsonb NOT NULL,
    fetched_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_operator_chat_messages UNIQUE (chat_id, provider_message_id)
);
CREATE INDEX IF NOT EXISTS idx_operator_chat_messages_chat_sent ON operator_chat_messages (chat_id, sent_at);

CREATE TABLE IF NOT EXISTS operator_chat_monitoring (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id              uuid NOT NULL REFERENCES operator_chats(id),
    monitor              boolean NOT NULL,
    decided_by           text NOT NULL,
    decided_at           timestamptz NOT NULL DEFAULT now(),
    via                  text NOT NULL CHECK (via IN ('manual', 'page', 'policy')),
    note                 text
);
CREATE INDEX IF NOT EXISTS idx_operator_chat_monitoring_chat ON operator_chat_monitoring (chat_id, decided_at DESC);

CREATE OR REPLACE VIEW v_operator_chat_monitoring AS
SELECT DISTINCT ON (m.chat_id)
       m.chat_id, c.operator_id, c.title, m.monitor, m.decided_by, m.decided_at, m.via, m.note
  FROM operator_chat_monitoring m
  JOIN operator_chats c ON c.id = m.chat_id
 ORDER BY m.chat_id, m.decided_at DESC, m.id DESC;
