#!/usr/bin/env python3
"""marketbase-register-operator

Register (or update) an outbound sending identity in a client's MarketBase.

Given just the client + Unipile account id, hits Unipile once to fill in
the display name, channel identifier (LinkedIn URN / Gmail addr), and
channel type. Optionally accepts --hubspot-contact-id to link back to the
HubSpot source-of-truth.

Idempotent — re-running with the same (channel, unipile_account_id) UPDATEs.

Usage:
  python3 register_operator.py --client Acme \\
    --unipile-account-id eoJxwsXBSYav9udcm3aV_Q \\
    --hubspot-contact-id 214210054770
  python3 register_operator.py --client Acme --list
  python3 register_operator.py --client Acme \\
    --unipile-account-id <id> --deactivate
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, load_client_env


def fetch_unipile_account(base_url: str, token: str, account_id: str) -> dict:
    """Find the account record by id. We use the LIST endpoint and filter
    client-side because some Unipile tenants 401 on the single-GET path
    even when the LIST works fine (scope quirk; same API key)."""
    url = f"{base_url.rstrip('/')}/api/v1/accounts?limit=200"
    req = urllib.request.Request(
        url, headers={"X-API-KEY": token, "accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    for a in data.get("items", []):
        if a.get("id") == account_id:
            return a
    sys.exit(f"No Unipile account with id={account_id} on this tenant.")


def derive_operator_fields(account: dict) -> dict:
    """Translate a Unipile account record into outbound_operators columns."""
    atype = (account.get("type") or "").upper()
    if atype == "LINKEDIN":
        cp = account.get("connection_params", {}).get("im", {}) or {}
        return {
            "display_name": account.get("name") or "(unknown)",
            "channel": "linkedin",
            "channel_identifier": cp.get("id") or "",
            "email": None,
        }
    if atype == "GOOGLE_OAUTH":
        # Gmail account — channel='gmail' for future use.
        cp = account.get("connection_params", {}).get("mail", {}) or {}
        return {
            "display_name": cp.get("username") or account.get("name") or "(unknown)",
            "channel": "gmail",
            "channel_identifier": cp.get("username") or "",
            "email": cp.get("username"),
        }
    sys.exit(f"Unsupported Unipile account type: {atype}")


def upsert_operator(conn, fields: dict) -> str:
    """INSERT or UPDATE by (channel, unipile_account_id). Returns the id."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO outbound_operators
              (display_name, email, hubspot_contact_id, channel,
               channel_identifier, unipile_account_id, is_active)
            VALUES (%(display_name)s, %(email)s, %(hubspot_contact_id)s,
                    %(channel)s, %(channel_identifier)s,
                    %(unipile_account_id)s, true)
            ON CONFLICT (channel, unipile_account_id) DO UPDATE
              SET display_name = EXCLUDED.display_name,
                  email = COALESCE(EXCLUDED.email, outbound_operators.email),
                  hubspot_contact_id = COALESCE(EXCLUDED.hubspot_contact_id, outbound_operators.hubspot_contact_id),
                  channel_identifier = EXCLUDED.channel_identifier,
                  is_active = true
            RETURNING id
        """, fields)
        conn.commit()
        return cur.fetchone()[0]


def list_operators(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT display_name, channel, channel_identifier, unipile_account_id,
                   is_active, added_at::date
            FROM outbound_operators
            ORDER BY added_at
        """)
        rows = cur.fetchall()
    if not rows:
        print("No outbound_operators registered.")
        return
    print(f"{'name':<22} {'ch':<8} {'identifier':<48} {'unipile_id':<28} {'active':<6} {'added':<12}")
    for r in rows:
        name, ch, ident, uid, active, added = r
        print(f"{name[:22]:<22} {ch:<8} {(ident or '')[:48]:<48} {uid[:28]:<28} "
              f"{'yes' if active else 'no':<6} {str(added):<12}")


def deactivate(conn, channel: str, account_id: str):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE outbound_operators SET is_active = false
            WHERE channel = %s AND unipile_account_id = %s
            RETURNING display_name
        """, (channel, account_id))
        row = cur.fetchone()
    conn.commit()
    if row:
        print(f"Deactivated: {row[0]}")
    else:
        print("No matching operator.")


def main() -> int:
    p = argparse.ArgumentParser(description="Register an outbound operator in a client's MarketBase.")
    p.add_argument("--client", required=True)
    p.add_argument("--unipile-account-id", help="Unipile account id to register.")
    p.add_argument("--hubspot-contact-id", default=None,
                   help="HubSpot contact id for source-of-truth linking.")
    p.add_argument("--channel", default="linkedin",
                   help="Override channel; default auto-derived from Unipile account type.")
    p.add_argument("--list", action="store_true", help="List registered operators.")
    p.add_argument("--deactivate", action="store_true",
                   help="Mark the operator inactive (paused/disconnected).")
    args = p.parse_args()

    env = load_client_env(args.client)
    # Use the CLIENT pair (UNIPILE_ACCESS_TOKEN + UNIPILE_BASE_SERVER) when
    # present. Don't mix client token with global UNIPILE_BASE_URL — they
    # point at different Unipile shards.
    if env.get("UNIPILE_ACCESS_TOKEN") and env.get("UNIPILE_BASE_SERVER"):
        token = env["UNIPILE_ACCESS_TOKEN"]
        base_url = f"https://{env['UNIPILE_BASE_SERVER']}"
    else:
        token = env.get("UNIPILE_API_KEY")
        base_url = env.get("UNIPILE_BASE_URL")

    with connect(args.client) as conn:
        if args.list:
            list_operators(conn); return 0
        if not args.unipile_account_id:
            sys.exit("--unipile-account-id is required (or --list).")
        if args.deactivate:
            deactivate(conn, args.channel, args.unipile_account_id); return 0

        if not token or not base_url:
            sys.exit("UNIPILE_ACCESS_TOKEN / UNIPILE_BASE_SERVER required in env.")
        account = fetch_unipile_account(base_url, token, args.unipile_account_id)
        fields = derive_operator_fields(account)
        fields["unipile_account_id"] = args.unipile_account_id
        fields["hubspot_contact_id"] = args.hubspot_contact_id
        op_id = upsert_operator(conn, fields)
        print(f"Registered: {fields['display_name']} ({fields['channel']})  id={op_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
