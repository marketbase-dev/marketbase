-- MarketBase schema, migration 023 — outbound conversations and messages
--
-- Captures the actual message history between our outbound operators
-- (Dana, cofounders, future team members) and individual leads, plus
-- inbound emoji reactions on our outbound messages. Generic across all
-- clients; written specifically against Unipile LinkedIn today, but the
-- `channel` columns allow Gmail / WhatsApp later without DDL.
--
-- Sync model: LEAD-TARGETED. The user picks specific leads to poll. We
-- do NOT auto-create unknown leads from chat-walk. Sync state lives
-- per-conversation (`lead_conversations.last_synced_at`), never global.
--
-- Auto-tagging side-effects (enforced by marketbase-sync-conversations):
--   * new inbound message  → apply `they:replied` + bump
--                            `campaign_members.status='replied'` (terminal-status guarded)
--   * new inbound reaction → apply `they:reacted_to_our_message`

-- ── outbound_operators ─────────────────────────────────────────────────────
-- Sending identities for THIS client. One row per (human × channel).
-- Dana's LinkedIn = one row; Dana's Gmail (when wired) = another.
CREATE TABLE IF NOT EXISTS outbound_operators (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name         text NOT NULL,                    -- 'Dana Tolts'
    email                text,                             -- 'dana@acme.com'
    hubspot_contact_id   text,                             -- link to HubSpot source-of-truth
    channel              text NOT NULL,                    -- 'linkedin' | 'gmail' | 'whatsapp'
    channel_identifier   text NOT NULL,                    -- LinkedIn URN | gmail addr | phone
    unipile_account_id   text NOT NULL,                    -- Unipile account id for this seat
    is_active            boolean NOT NULL DEFAULT true,
    added_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_operators_channel_unipile UNIQUE (channel, unipile_account_id),
    CONSTRAINT uq_operators_channel_identifier UNIQUE (channel, channel_identifier)
);

-- ── lead_conversations ─────────────────────────────────────────────────────
-- One row per (operator, lead, channel) thread. A lead can have parallel
-- threads with different operators — query by lead_id to surface all.
CREATE TABLE IF NOT EXISTS lead_conversations (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id          uuid NOT NULL REFERENCES outbound_operators(id) ON DELETE CASCADE,
    lead_id              uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    channel              text NOT NULL,                    -- mirrors operator.channel
    provider_chat_id     text NOT NULL,                    -- Unipile chat id
    first_message_at     timestamptz,
    last_message_at      timestamptz,
    last_synced_at       timestamptz,                      -- PER-CONVERSATION sync state
    last_sync_error      text,
    message_count        integer NOT NULL DEFAULT 0,
    reaction_count       integer NOT NULL DEFAULT 0,
    CONSTRAINT uq_lconv_channel_chat UNIQUE (channel, provider_chat_id),
    CONSTRAINT uq_lconv_operator_lead_channel UNIQUE (operator_id, lead_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_lconv_lead     ON lead_conversations(lead_id);
CREATE INDEX IF NOT EXISTS idx_lconv_operator ON lead_conversations(operator_id, last_message_at DESC);

-- ── lead_messages ──────────────────────────────────────────────────────────
-- Append-only message log. Never edit, never delete (cascades only).
CREATE TABLE IF NOT EXISTS lead_messages (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id      uuid NOT NULL REFERENCES lead_conversations(id) ON DELETE CASCADE,
    provider_message_id  text NOT NULL,                    -- Unipile `provider_id` — idempotency key
    direction            text NOT NULL CHECK (direction IN ('outbound','inbound')),
    sent_at              timestamptz NOT NULL,
    body                 text,                             -- no cap
    raw                  jsonb,                            -- full Unipile payload — replay-friendly
    fetched_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_lmsg_conv_provider UNIQUE (conversation_id, provider_message_id)
);
CREATE INDEX IF NOT EXISTS idx_lmsg_conv_sent ON lead_messages(conversation_id, sent_at);

-- ── lead_message_reactions ─────────────────────────────────────────────────
-- Emoji reactions on individual messages. Unipile exposes reactions inline
-- on the message object (no second API call) but provides NO timestamp —
-- only `value` (emoji), `is_sender` (which side reacted), and `sender_id`
-- (reactor URN). We store `fetched_at` as our own "first seen" marker.
--
-- The unique constraint makes ON CONFLICT DO NOTHING the discovery primitive:
-- rowcount = 1 means a genuinely new reaction; the syncer fires the
-- `they:reacted_to_our_message` tag exactly when that happens on an
-- inbound reaction landing on an outbound message.
CREATE TABLE IF NOT EXISTS lead_message_reactions (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id           uuid NOT NULL REFERENCES lead_messages(id) ON DELETE CASCADE,
    reactor_provider_id  text NOT NULL,                    -- LinkedIn URN of reactor
    reactor_direction    text NOT NULL CHECK (reactor_direction IN ('outbound','inbound')),
    reaction_value       text NOT NULL,                    -- emoji (👍, ❤️, 🙏, ...)
    fetched_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_lreact_message_reactor_value UNIQUE (message_id, reactor_provider_id, reaction_value)
);
CREATE INDEX IF NOT EXISTS idx_lreact_message ON lead_message_reactions(message_id);
