#!/usr/bin/env python3
"""log_meeting.py — record (or reschedule/cancel) a scheduled meeting for a lead.

Writes to the `lead_meetings` table (schema migration 026) so the
pending-conversations review can surface T-4 / T-2 / T-0 reminders.

Examples
--------
# Log a confirmed call (absolute time given in a local zone):
  python3 log_meeting.py --client Acme \
    --lead-url https://www.linkedin.com/in/<urn> \
    --at "2026-06-10 12:00" --tz America/New_York \
    --host dana --title "Acme <> Majesco - Ernest Oporto" \
    --source calendar:unipile --notes "self-booked via Calendly, both accepted"

# Mark the current upcoming meeting cancelled / rescheduled:
  python3 log_meeting.py --client Acme --lead-url <url> --cancel
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, normalize_linkedin_url  # noqa: E402

VALID_STATUS = ("scheduled", "held", "cancelled", "no_show", "rescheduled")


def parse_when(at: str, tz: str | None) -> datetime:
    """Parse --at into an aware datetime. If --at carries no offset, localize
    it with --tz (required in that case)."""
    s = at.strip().replace("Z", "+00:00")
    # Allow "YYYY-MM-DD HH:MM" as well as ISO 'T' form.
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.fromisoformat(s.replace(" ", "T"))
    if dt.tzinfo is None:
        if not tz:
            sys.exit("--at has no timezone offset; pass --tz <IANA> (e.g. America/New_York)")
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt


def main() -> None:
    ap = argparse.ArgumentParser(description="Record a scheduled meeting for a lead.")
    ap.add_argument("--client", required=True)
    ap.add_argument("--lead-url", required=True)
    ap.add_argument("--at", help="Meeting start, ISO or 'YYYY-MM-DD HH:MM'. Required unless --cancel.")
    ap.add_argument("--tz", help="IANA tz to localize --at when it has no offset (also stored for display).")
    ap.add_argument("--host", help="dana | bob | eyar")
    ap.add_argument("--title")
    ap.add_argument("--campaign", help="Campaign name to link (optional).")
    ap.add_argument("--source", default="manual", help="calendar:unipile | calendly | manual")
    ap.add_argument("--calendar-event-id")
    ap.add_argument("--notes")
    ap.add_argument("--created-by", default="log_meeting.py")
    ap.add_argument("--cancel", action="store_true",
                    help="Mark the lead's current scheduled meeting cancelled instead of inserting.")
    ap.add_argument("--mark-reminder", choices=("T-4", "T-2", "T-0"),
                    help="Log that this reminder was sent for the lead's current scheduled meeting "
                         "(so the review stops flagging it). Use after sending a reminder.")
    ap.add_argument("--message-id", help="Unipile message_id to record with --mark-reminder.")
    ap.add_argument("--status", default="scheduled", choices=VALID_STATUS)
    args = ap.parse_args()

    url = normalize_linkedin_url(args.lead_url)
    with connect(args.client) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM leads WHERE linkedin_url = %s", (url,))
            row = cur.fetchone()
            if not row:
                sys.exit(f"lead not found in MarketBase: {url}  (ingest it first with marketbase-upload-leads)")
            lead_id, name = row

            if args.mark_reminder:
                entry = {"kind": args.mark_reminder,
                         "at": datetime.now(timezone.utc).isoformat()}
                if args.message_id:
                    entry["message_id"] = args.message_id
                cur.execute("""
                    UPDATE lead_meetings
                    SET reminders_sent = reminders_sent || %s::jsonb
                    WHERE lead_id=%s AND status='scheduled'
                    RETURNING scheduled_at
                """, (json.dumps([entry]), lead_id))
                got = cur.fetchall()
                conn.commit()
                print(f"logged {args.mark_reminder} reminder for {name} "
                      f"({len(got)} scheduled meeting(s) updated)")
                return

            if args.cancel:
                cur.execute("""
                    UPDATE lead_meetings SET status='cancelled'
                    WHERE lead_id=%s AND status='scheduled'
                    RETURNING scheduled_at
                """, (lead_id,))
                got = cur.fetchall()
                conn.commit()
                print(f"cancelled {len(got)} scheduled meeting(s) for {name}")
                return

            if not args.at:
                sys.exit("--at is required when not using --cancel")
            when = parse_when(args.at, args.tz)

            campaign_id = None
            if args.campaign:
                cur.execute("SELECT id FROM campaigns WHERE name=%s", (args.campaign,))
                c = cur.fetchone()
                campaign_id = c[0] if c else None

            # Supersede any existing scheduled meeting for this lead (a reschedule).
            cur.execute("""
                UPDATE lead_meetings SET status='rescheduled'
                WHERE lead_id=%s AND status='scheduled' AND scheduled_at <> %s
            """, (lead_id, when))

            cur.execute("""
                INSERT INTO lead_meetings
                    (lead_id, campaign_id, scheduled_at, scheduled_tz, host, title,
                     status, source, calendar_event_id, created_by, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING id
            """, (lead_id, campaign_id, when, args.tz, args.host, args.title,
                  args.status, args.source, args.calendar_event_id, args.created_by, args.notes))
            ins = cur.fetchone()
            conn.commit()

    disp = when.astimezone(ZoneInfo(args.tz)) if args.tz else when
    print(f"logged meeting for {name}: {disp:%a %Y-%m-%d %H:%M %Z}  host={args.host}  status={args.status}")


if __name__ == "__main__":
    main()
