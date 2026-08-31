#!/usr/bin/env python3
"""marketbase-send-message

Send a LinkedIn (or other-channel) message to a lead via Unipile, from the
correct outbound operator's account.

This is the *outbound* counterpart to sync_conversations.py. It resolves the
operator↔lead chat from `lead_conversations` (so we reply inside the existing
thread, from the operator who actually owns it) and POSTs the text to Unipile's
`/api/v1/chats/{chat_id}/messages` endpoint.

Sending is authorized outreach (NOT scraping) — the only sanctioned use of
Unipile per the workspace rules. Always invoked behind a human-approved draft.

Usage:
  set -a; source ~/.env.Acme; set +a

  # Reply in the existing thread (operator auto-resolved if the lead has exactly
  # one linkedin conversation; otherwise pass --operator):
  python3 send_message.py --client Acme \
    --lead-url https://www.linkedin.com/in/ACoAA... \
    --text "No problem at all, ..."

  # Disambiguate the sender when a lead has chats with multiple operators:
  python3 send_message.py --client Acme --lead-url https://… \
    --operator "Eyar Zilberman" --text "..."

  # Preview without sending:
  python3 send_message.py --client Acme --lead-url https://… \
    --text "..." --dry-run
"""
from __future__ import annoacmens

import argparse
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, normalize_linkedin_url


def unipile_base_and_token() -> tuple[str, str]:
    """Mirror sync_conversations.py: prefer the per-client CLIENT pair."""
    if os.environ.get("UNIPILE_ACCESS_TOKEN") and os.environ.get("UNIPILE_BASE_SERVER"):
        return f"https://{os.environ['UNIPILE_BASE_SERVER']}", os.environ["UNIPILE_ACCESS_TOKEN"]
    token = os.environ.get("UNIPILE_API_KEY")
    base = os.environ.get("UNIPILE_BASE_URL")
    if not token or not base:
        sys.exit("UNIPILE_ACCESS_TOKEN + UNIPILE_BASE_SERVER missing from env. "
                 "Did you `set -a; source ~/.env.<Client>; set +a`?")
    return base, token


def resolve_chat(conn, lead_url: str, operator: str | None, channel: str):
    """Return (operator_display_name, unipile_account_id, provider_chat_id).
    Errors clearly when the lead has zero or ambiguous conversations."""
    normed = normalize_linkedin_url(lead_url)
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM leads WHERE linkedin_url = %s", (normed,))
        row = cur.fetchone()
        if not row:
            sys.exit(f"no lead in MarketBase for {normed}")
        lead_id, lead_name = row

        q = """
            SELECT o.display_name, o.unipile_account_id, lc.provider_chat_id
            FROM lead_conversations lc
            JOIN outbound_operators o ON o.id = lc.operator_id
            WHERE lc.lead_id = %s AND lc.channel = %s
              AND lc.provider_chat_id IS NOT NULL
        """
        params = [lead_id, channel]
        if operator:
            q += " AND o.display_name = %s"
            params.append(operator)
        cur.execute(q, params)
        rows = cur.fetchall()

    if not rows:
        sys.exit(f"no {channel} conversation found for {lead_name} "
                 f"{'with operator ' + operator if operator else ''}— "
                 f"run sync_conversations.py first, or pass --chat-id.")
    if len(rows) > 1:
        names = ", ".join(r[0] for r in rows)
        sys.exit(f"{lead_name} has conversations with multiple operators ({names}). "
                 f"Pass --operator to choose the sender.")
    return rows[0]


def send(base: str, token: str, chat_id: str, text: str) -> dict:
    """POST the message to Unipile. Multipart form-data with a `text` field."""
    url = f"{base}/api/v1/chats/{chat_id}/messages"
    resp = requests.post(
        url,
        headers={"X-API-KEY": token, "accept": "application/json"},
        files={"text": (None, text)},  # (None, value) => multipart form field
        timeout=30,
    )
    if resp.status_code >= 300:
        sys.exit(f"Unipile send failed [{resp.status_code}]: {resp.text}")
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


def main() -> int:
    ap = argparse.ArgumentParser(description="Send a message to a lead via Unipile.")
    ap.add_argument("--client", required=True)
    ap.add_argument("--lead-url", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="Message body.")
    g.add_argument("--text-file", help="Read the message body from this file.")
    ap.add_argument("--operator", help="Operator display_name to send as (disambiguates).")
    ap.add_argument("--chat-id", help="Override the provider chat id (skips lookup).")
    ap.add_argument("--channel", default="linkedin")
    ap.add_argument("--dry-run", action="store_true", help="Print, don't send.")
    args = ap.parse_args()

    text = args.text if args.text is not None else Path(args.text_file).read_text()
    text = text.rstrip("\n")
    if not text.strip():
        sys.exit("refusing to send an empty message.")

    base, token = unipile_base_and_token()

    if args.chat_id:
        op_name, account_id, chat_id = (args.operator or "?"), None, args.chat_id
    else:
        with connect(args.client) as conn:
            op_name, account_id, chat_id = resolve_chat(
                conn, args.lead_url, args.operator, args.channel)

    print(f"  sender   : {op_name}  (account {account_id})")
    print(f"  chat_id  : {chat_id}")
    print(f"  channel  : {args.channel}")
    print(f"  message  :\n----------\n{text}\n----------")

    if args.dry_run:
        print("  DRY-RUN — nothing sent.")
        return 0

    result = send(base, token, chat_id, text)
    msg_id = result.get("message_id") or result.get("id") or result
    print(f"  ✅ SENT. unipile message id: {msg_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
