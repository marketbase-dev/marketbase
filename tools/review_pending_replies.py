#!/usr/bin/env python3
"""acme-review-pending-conversations (pre-check phase)

Batch orchestrator phase: produce the prioritized queue of pending prospect
conversations that need our attention.

Workflow (the SKILL.md ties these together):
  1. (this script) — sync all `they:replied` leads from Unipile, then
     categorize + prioritize, with qualification pre-checks.
  2. The agent walks the queue. For each lead, the agent invokes
     `acme-propose-reply` (Layer B) to draft a single response.
  3. For snoozed / reservoir leads past `not_before`, this script ALSO
     surfaces them — the agent then proposes a nudge (even though the
     prospect hasn't replied) via the same propose-reply skill's nudge
     templates.

Buckets:
  A. AWAITING_OUR_RESPONSE       — last message inbound, we haven't replied
  B. NUDGE_24H_NO_REPLY          — our last outbound carried a specific ask
                                   (`we:sent_them_possible_times`,
                                   `we:sent_calendar_invite`,
                                   `we:sent_referral_ask`) and 24h+ has passed
                                   with no inbound
  C. SNOOZE_PAST_DUE             — `plan:snooze` with `notes` date <= today
                                   and no inbound since (nudge candidate)
  D. RESERVOIR_PAST_DUE          — in `referralDiscovery_*_nameDropReservoir_*`
                                   campaign with `not_before` <= today
                                   (graduation candidate; ice-breaker stored
                                   in `campaign_members.notes`)
  E. JUST_SENT (<24h)            — we sent something <24h ago, wait
  F. CLOSED_OUT                  — `we:thanked_for_declining` or terminal
                                   campaign status; skip
  G. MEETING_BOOKED              — skip (or surface for prep separately)
  H. STALE                       — outbound 72h+ ago with no specific ask;
                                   probably dead

Pre-check flags (surfaced per lead, regardless of bucket):
  - uses_competitor          : current_company has `bought_competitor_product`
                                relationship → likely DQ candidate
  - is_competitor            : current_company is itself a competitor → DQ
  - is_security_vendor       : current_company is in `security_vendor` list → DQ
  - out_of_geo_indonesia     : country in {India, Pakistan, Bangladesh,
                                Indonesia, Vietnam, Philippines, Thailand,
                                Malaysia, Sri Lanka, Nepal} → likely DQ
  - swan_only_qual           : currently `qual:qualified` but no manual
                                qualification — Smartlead-decided only, worth
                                a human re-check

Priority ordering (within a bucket):
  - qual:senior_ic         most senior
  - qual:qualified
  - qual:pending
  - qual:networker
  (then by hours_since_last_inbound DESC — older first within tier)

Usage:
    set -a; source ~/.env.Acme; set +a
    python3 review_pending_replies.py --client Acme \\
        [--bucket A,B,C,D]           # default: all that need action
        [--no-sync]                  # skip the upfront sync (faster, stale)
        [--top N]                    # default unlimited
        [--csv /tmp/queue.csv]       # also dump as CSV
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect


# Meeting-reminder cadence: each kind fires once its window has opened (days
# until the call <= threshold) and it hasn't already been logged in
# lead_meetings.reminders_sent. Tightest un-sent reminder wins, so a daily run
# naturally walks T-4 -> T-2 -> T-0, and a missed window jumps to the current one.
REMINDER_CADENCE = [  # (kind, threshold_days, template_label)
    ("T-0", 0.5, "day-of confirm"),
    ("T-2", 2.5, "2-day confirm + logistics"),
    ("T-4", 4.5, "4-day soft check-in"),
]


def print_meetings(conn) -> None:
    """Surface upcoming scheduled meetings and flag which ones are due a
    T-4 / T-2 / T-0 reminder (see acme-propose-reply -> Meeting reminders
    for the templates). No-op if the lead_meetings table isn't present."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT l.name, l.linkedin_url, m.id, m.scheduled_at, m.scheduled_tz,
                       m.host, m.title, m.reminders_sent, m.calendar_event_id,
                       EXTRACT(EPOCH FROM (m.scheduled_at - now())) / 86400.0 AS days_until
                FROM lead_meetings m
                JOIN leads l ON l.id = m.lead_id
                WHERE m.status = 'scheduled' AND m.scheduled_at >= now()
                ORDER BY m.scheduled_at ASC
            """)
            rows = cur.fetchall()
    except Exception:
        conn.rollback()
        return  # table not migrated for this client yet

    if not rows:
        return

    # As the slot approaches (T-2 window), a meeting that still has NO reminder
    # logged is at risk: it usually means the reminder cadence never ran, and
    # (per the Jos Gubbels / AUNDE no-show, 2026-07-16) an invite that was never
    # actually delivered slips through silently. Pair it with an unlinked-event
    # check so an invite that was never created (calendar_event_id NULL) surfaces
    # BEFORE the call rather than becoming a no-show.
    NEAR_SLOT_DAYS = 2.5  # T-2 window
    at_risk = []
    print("\n=== MEETINGS (upcoming) ===\n")
    for (name, url, _mid, sched_at, tz, host, title, reminders_sent,
         calendar_event_id, days_until) in rows:
        sent_kinds = {r.get("kind") for r in (reminders_sent or [])}
        due = None
        for kind, thresh, label in REMINDER_CADENCE:
            if days_until <= thresh and kind not in sent_kinds:
                due = (kind, label)
                break
        local = sched_at.astimezone(ZoneInfo(tz)) if tz else sched_at
        when = f"{local:%a %b %-d, %-I:%M %p %Z}".rstrip()
        flag = f"  ⏰ REMINDER DUE: {due[0]} ({due[1]})" if due else ""

        # Deliverability health flags (independent of the reminder-due flag).
        health = []
        if not calendar_event_id:
            health.append("invite not linked in MarketBase — confirm it was created and reached the prospect")
        if days_until <= NEAR_SLOT_DAYS and not (reminders_sent or []):
            health.append("no reminders logged as the slot nears — invite delivery unverified")
        health_flag = f"  🚑 AT RISK: {'; '.join(health)}" if health else ""

        host_s = f"host={host}" if host else ""
        print(f"  {when}  ({days_until:.1f}d)  {name}  {host_s}{flag}{health_flag}")
        print(f"               {title or ''}")
        print(f"               url: {url}")
        if health:
            at_risk.append((name, when, health))
    print()

    if at_risk:
        print("🚑 AT-RISK BOOKINGS (verify the invite reached the prospect before the call):")
        for name, when, health in at_risk:
            print(f"   - {name} ({when}): {'; '.join(health)}")
        print()


# ICP geo blocks — leads here are likely out-of-geo per the Madhuprasad pattern
OUT_OF_GEO_COUNTRIES = {
    "India", "Pakistan", "Bangladesh", "Sri Lanka", "Nepal",
    "Indonesia", "Vietnam", "Philippines", "Thailand", "Malaysia",
}

# Buckets that need action vs. ignore by default
ACTION_BUCKETS = {"A", "B", "C", "D"}


def run_urn_backfill(client: str) -> None:
    """Self-heal step, runs FIRST every pass. Smartlead flags fresh repliers
    (`they:replied`) but doesn't enrich them, so they often land with a vanity
    URL and no LinkedIn URN — which makes them unsyncable (the conversation sync
    matches Unipile chats on the `ACoAA…` URN). This resolves the URN via Fresh
    Profile Data `/enrich-lead` and backfills `leads.linkedin_urn` so they become
    syncable THIS run. Read-through cached in `enrichment_calls`, so re-runs only
    pay for net-new leads. Duplicates of an existing URN-form lead are flagged
    `flag:duplicate_lead` and skipped. Non-fatal — a bad enrich run never blocks
    triage."""
    print("Enriching newly-replied leads missing a URN (Smartlead-flagged, not yet "
          "enriched)...", flush=True)
    subprocess.run([
        "python3", str(Path(__file__).resolve().parent / "backfill_urn_from_enrich.py"),
        "--client", client,
    ], check=False)


def run_sync(client: str, urls: list[str], workers: int = 1) -> None:
    if not urls:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     encoding="utf-8", newline="") as f:
        f.write("linkedin_url\n")
        for u in urls:
            f.write(u + "\n")
        path = f.name
    print(f"Syncing {len(urls)} replied leads from Unipile "
          f"[workers={workers}]...", flush=True)
    subprocess.run([
        "python3", str(Path(__file__).resolve().parent / "sync_conversations.py"),
        "--client", client, "--lead-file", path,
        "--workers", str(workers),
    ], check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--client", required=True)
    ap.add_argument("--bucket", default="A,B,C,D",
                    help="Comma-separated buckets to surface (default A,B,C,D).")
    ap.add_argument("--no-sync", action="store_true",
                    help="Skip the upfront Unipile sync — fast, but possibly stale.")
    ap.add_argument("--workers", type=int, default=1,
                    help="Sync this many leads concurrently (default 1). Leads are "
                         "independent; 6-10 is a good range against Unipile.")
    ap.add_argument("--no-backfill-urns", action="store_true",
                    help="Skip the upfront URN-enrichment self-heal step (resolves "
                         "Smartlead-flagged repliers that have a vanity URL and no URN).")
    ap.add_argument("--top", type=int, default=0,
                    help="Limit to top N (0 = no limit).")
    ap.add_argument("--csv", help="Also dump queue to this CSV path.")
    args = ap.parse_args()

    wanted_buckets = {b.strip().upper() for b in args.bucket.split(",")}

    # Step 0 (very first thing): self-heal newly-flagged repliers that Smartlead
    # hasn't enriched, so they're URN-resolvable before the sync runs below.
    if not args.no_backfill_urns:
        run_urn_backfill(args.client)

    # Only sync leads that could actually appear in a REQUESTED bucket. A lead
    # carrying a terminal tag is forced by the triage into F (qual:disqualified
    # / manually_disqualified:% / we:thanked_for_declining) or G
    # (they:accepted_calendar_invite) no matter what new message arrives — so
    # syncing it is wasted Unipile work unless that bucket was explicitly asked
    # for. We exclude it from the SYNC list only; the triage SQL below still
    # reads every they:replied lead, so `--bucket F` etc. continues to surface
    # them (just not freshly synced, which is fine for an audit). On a typical
    # Acme run this skips ~25% of the leads (the DQ'd/closed/booked tail).
    exclude_tags: list[str] = []
    exclude_like: list[str] = []
    if "F" not in wanted_buckets:
        exclude_tags += ["qual:disqualified", "we:thanked_for_declining"]
        exclude_like += ["manually_disqualified:%"]
    if "G" not in wanted_buckets:
        exclude_tags += ["they:accepted_calendar_invite"]

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(DISTINCT lead_id) FROM lead_tags "
                        "WHERE tag='they:replied'")
            total_replied = cur.fetchone()[0]

            where_excl, params = "", []
            if exclude_tags or exclude_like:
                conds = []
                if exclude_tags:
                    conds.append("x.tag = ANY(%s)")
                    params.append(exclude_tags)
                for pat in exclude_like:
                    conds.append("x.tag LIKE %s")
                    params.append(pat)
                where_excl = (
                    " AND l.id NOT IN (SELECT x.lead_id FROM lead_tags x WHERE "
                    + " OR ".join(conds) + ")"
                )
            cur.execute(
                "SELECT DISTINCT l.linkedin_url "
                "FROM leads l JOIN lead_tags lt ON lt.lead_id=l.id "
                "WHERE lt.tag='they:replied'" + where_excl,
                params,
            )
            urls = [r[0] for r in cur.fetchall()]
            skipped = total_replied - len(urls)
            note = (f" ({skipped} terminal DQ/closed/booked leads skipped — "
                    f"not in requested buckets)") if skipped else ""
            print(f"{total_replied} leads tagged they:replied; "
                  f"{len(urls)} to sync{note}.", flush=True)

    if not args.no_sync:
        run_sync(args.client, urls, workers=args.workers)

    # === Phase 2: triage SQL ===
    triage_sql = """
WITH replied AS (
  SELECT DISTINCT lt.lead_id FROM lead_tags lt WHERE lt.tag='they:replied'
),
last_msg AS (
  SELECT lc.lead_id, lm.direction, lm.sent_at, lm.body,
         ROW_NUMBER() OVER (PARTITION BY lc.lead_id
                            ORDER BY lm.sent_at DESC) AS rn
  FROM lead_messages lm JOIN lead_conversations lc ON lc.id=lm.conversation_id
  WHERE lc.lead_id IN (SELECT lead_id FROM replied)
),
tags_agg AS (
  SELECT lt.lead_id,
    bool_or(lt.tag='qual:senior_ic')          AS is_senior_ic,
    bool_or(lt.tag='qual:qualified')           AS is_qualified,
    bool_or(lt.tag='qual:pending')             AS is_pending,
    bool_or(lt.tag='qual:disqualified'
            OR lt.tag LIKE 'manually_disqualified:%')  AS is_disqualified,
    bool_or(lt.tag='qual:networker')           AS is_networker,
    bool_or(lt.tag='plan:snooze')              AS has_snooze,
    MAX(CASE WHEN lt.tag='plan:snooze' THEN lt.notes END)
                                                AS snooze_until,
    bool_or(lt.tag='plan:name_drop_reservoir') AS in_reservoir,
    bool_or(lt.tag='plan:book_meeting')        AS plan_book_meeting,
    bool_or(lt.tag='plan:book_with_eyar')      AS plan_book_with_eyar,
    bool_or(lt.tag='plan:nurture')             AS plan_nurture,
    bool_or(lt.tag='plan:check_fit')           AS plan_check_fit,
    bool_or(lt.tag='plan:pass')                AS plan_pass,
    bool_or(lt.tag='plan:partnership_play')    AS plan_partnership_play,
    bool_or(lt.tag='plan:let_go')              AS plan_let_go,
    bool_or(lt.tag='they:accepted_calendar_invite') AS accepted_invite,
    bool_or(lt.tag='we:thanked_for_declining') AS closed_out,
    bool_or(lt.tag='we:sent_them_possible_times'
            OR lt.tag='we:sent_calendar_invite'
            OR lt.tag='we:sent_referral_ask')   AS sent_specific_ask,
    bool_or(lt.tag='we:sent_them_possible_times') AS we_sent_times,
    bool_or(lt.tag='we:sent_calendar_invite')  AS we_sent_invite,
    bool_or(lt.tag='we:sent_referral_ask')     AS we_sent_referral,
    bool_or(lt.tag='we:nudged')                AS we_nudged,
    bool_or(lt.tag='they:asked_technical_question') AS asked_tech_q,
    bool_or(lt.tag='they:asked_about_scope')   AS asked_scope,
    bool_or(lt.tag='they:gave_deep_feedback')  AS deep_feedback,
    bool_or(lt.tag='they:willing_to_give_feedback') AS willing,
    bool_or(lt.tag='they:skeptical_but_engaged') AS skeptical_engaged,
    bool_or(lt.tag='they:declined_to_give_feedback') AS declined,
    bool_or(lt.tag='they:not_interested')      AS not_interested,
    bool_or(lt.tag='they:redirected_to_colleague') AS redirected,
    bool_or(lt.tag='they:politely_greeted')    AS politely_greeted,
    bool_or(lt.tag LIKE 'objection:%')          AS has_objection
  FROM lead_tags lt
  WHERE lt.lead_id IN (SELECT lead_id FROM replied)
  GROUP BY lt.lead_id
),
reservoir AS (
  SELECT cm.lead_id,
         (regexp_match(cm.notes, 'not_before:\\s*(\\d{4}-\\d{2}-\\d{2})'))[1]::date
                                                AS reservoir_not_before,
         (regexp_match(cm.notes, 'ice_breaker:\\s*(.*)$', 'n'))[1]
                                                AS reservoir_ice_breaker
  FROM campaign_members cm
  JOIN campaigns c ON c.id=cm.campaign_id
  WHERE c.name LIKE 'referralDiscovery_%nameDropReservoir%'
    AND cm.status='staged'
),
company_flags AS (
  SELECT l.id AS lead_id,
    bool_or(cr.relationship='bought_competitor_product') AS uses_competitor,
    bool_or(cr.relationship='competitor')                AS is_competitor,
    bool_or(cr.relationship='security_vendor')           AS is_security_vendor
  FROM leads l
  LEFT JOIN companies c ON c.name=l.current_company
  LEFT JOIN company_relationships cr ON cr.company_id=c.id
  WHERE l.id IN (SELECT lead_id FROM replied)
  GROUP BY l.id
)
SELECT
  l.linkedin_url, l.name, l.current_title, l.current_company,
  l.country, l.city,
  lm.direction AS last_dir,
  to_char(lm.sent_at AT TIME ZONE 'America/New_York',
          'YYYY-MM-DD HH24:MI "ET"')             AS last_msg_at,
  ROUND(EXTRACT(EPOCH FROM (now()-lm.sent_at))/3600, 1) AS hours_since,
  lm.body                                        AS last_body,
  COALESCE(ta.is_senior_ic, false)               AS is_senior_ic,
  COALESCE(ta.is_qualified, false)               AS is_qualified,
  COALESCE(ta.is_pending, false)                 AS is_pending,
  COALESCE(ta.is_disqualified, false)            AS is_disqualified,
  COALESCE(ta.is_networker, false)               AS is_networker,
  COALESCE(ta.has_snooze, false)                 AS has_snooze,
  ta.snooze_until,
  COALESCE(ta.in_reservoir, false)               AS in_reservoir,
  r.reservoir_not_before,
  COALESCE(ta.accepted_invite, false)            AS accepted_invite,
  COALESCE(ta.closed_out, false)                 AS closed_out,
  COALESCE(ta.sent_specific_ask, false)          AS sent_specific_ask,
  COALESCE(ta.plan_book_meeting, false)          AS plan_book_meeting,
  COALESCE(ta.plan_book_with_eyar, false)        AS plan_book_with_eyar,
  COALESCE(ta.plan_nurture, false)               AS plan_nurture,
  COALESCE(ta.plan_check_fit, false)             AS plan_check_fit,
  COALESCE(ta.plan_pass, false)                  AS plan_pass,
  COALESCE(ta.plan_partnership_play, false)      AS plan_partnership_play,
  COALESCE(ta.plan_let_go, false)                AS plan_let_go,
  COALESCE(ta.we_sent_times, false)              AS we_sent_times,
  COALESCE(ta.we_sent_invite, false)             AS we_sent_invite,
  COALESCE(ta.we_sent_referral, false)           AS we_sent_referral,
  COALESCE(ta.we_nudged, false)                  AS we_nudged,
  COALESCE(ta.asked_tech_q, false)               AS asked_tech_q,
  COALESCE(ta.asked_scope, false)                AS asked_scope,
  COALESCE(ta.deep_feedback, false)              AS deep_feedback,
  COALESCE(ta.willing, false)                    AS willing,
  COALESCE(ta.skeptical_engaged, false)          AS skeptical_engaged,
  COALESCE(ta.declined, false)                   AS declined,
  COALESCE(ta.not_interested, false)             AS not_interested,
  COALESCE(ta.redirected, false)                 AS redirected,
  COALESCE(ta.politely_greeted, false)           AS politely_greeted,
  COALESCE(ta.has_objection, false)              AS has_objection,
  COALESCE(cf.uses_competitor, false)            AS uses_competitor,
  COALESCE(cf.is_competitor, false)              AS is_competitor,
  COALESCE(cf.is_security_vendor, false)         AS is_security_vendor
FROM leads l
JOIN last_msg lm ON lm.lead_id=l.id AND lm.rn=1
LEFT JOIN tags_agg ta ON ta.lead_id=l.id
LEFT JOIN reservoir r ON r.lead_id=l.id
LEFT JOIN company_flags cf ON cf.lead_id=l.id
WHERE l.id IN (SELECT lead_id FROM replied)
ORDER BY lm.sent_at DESC
"""
    with connect(args.client) as conn:
        with conn.cursor() as cur:
            cur.execute(triage_sql)
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # === Phase 3: categorize + score ===
    today = __import__("datetime").date.today()
    bucketed: list[dict] = []
    for r in rows:
        flags: list[str] = []
        hard_dq = False
        if r["uses_competitor"]:
            flags.append("uses_competitor")
            hard_dq = True
        if r["is_competitor"]:
            flags.append("is_competitor")
            hard_dq = True
        if r["is_security_vendor"]:
            flags.append("is_security_vendor")
            hard_dq = True
        if r["country"] in OUT_OF_GEO_COUNTRIES:
            flags.append(f"out_of_geo:{r['country']}")

        # Bucket
        if r["accepted_invite"]:
            bucket = "G_meeting_booked"
        elif r["closed_out"]:
            bucket = "F_closed_out"
        elif r["is_disqualified"]:
            bucket = "F_closed_out"
        elif hard_dq:
            bucket = "X_needs_cleanup"
        elif r["has_snooze"] and r["snooze_until"] and r["snooze_until"] <= str(today):
            bucket = "C_snooze_past_due"
        elif r["in_reservoir"] and r["reservoir_not_before"] and \
                r["reservoir_not_before"] <= today:
            bucket = "D_reservoir_past_due"
        elif r["last_dir"] == "inbound":
            bucket = "A_awaiting_our_response"
        elif r["last_dir"] == "outbound" and r["hours_since"] < 24:
            bucket = "E_just_sent"
        elif r["last_dir"] == "outbound" and r["sent_specific_ask"] \
                and r["hours_since"] >= 24 \
                and not r["plan_nurture"] and not r["we_nudged"] \
                and not r["has_snooze"]:
            # Don't surface for nudging if we've already moved the lead to a
            # passive plan (plan:nurture), already used our one follow-up
            # (we:nudged), or deliberately snoozed it to a future date
            # (plan:snooze — a future snooze fell through the C check above, so
            # exclude it here too). They re-surface only if THEY reply (→
            # last_dir inbound → A) or when the snooze date arrives (→ C).
            bucket = "B_nudge_eligible"
        else:
            bucket = "H_stale"

        # === book_score: how likely is this lead to convert into a booked
        # meeting if we respond RIGHT NOW? Higher = act on first. ===
        score = 0.0
        notes: list[str] = []

        # Qualification base weight
        if r["is_senior_ic"]:
            score += 30; notes.append("+30 senior_ic")
        elif r["is_qualified"]:
            score += 20; notes.append("+20 qualified")
        elif r["is_pending"]:
            score += 10; notes.append("+10 pending")
        elif r["is_networker"]:
            score += 5; notes.append("+5 networker")

        # Engagement signals (positive)
        if r["asked_tech_q"]:        score += 25; notes.append("+25 asked_technical_question")
        if r["asked_scope"]:         score += 20; notes.append("+20 asked_about_scope")
        if r["deep_feedback"]:       score += 20; notes.append("+20 gave_deep_feedback")
        if r["skeptical_engaged"]:   score += 15; notes.append("+15 skeptical_but_engaged")
        if r["willing"]:             score += 10; notes.append("+10 willing_to_give_feedback")
        if r["has_objection"]:       score += 10; notes.append("+10 has_objection (engaged)")

        # Engagement signals (negative)
        if r["declined"]:            score -= 50; notes.append("-50 declined_to_give_feedback")
        if r["not_interested"]:      score -= 50; notes.append("-50 not_interested")
        if r["redirected"]:          score -= 10; notes.append("-10 redirected_to_colleague")

        # Plan state
        if r["plan_book_meeting"]:   score += 25; notes.append("+25 plan:book_meeting")
        if r["plan_book_with_eyar"]: score += 25; notes.append("+25 plan:book_with_eyar")
        if r["plan_check_fit"]:      score += 0;  # neutral
        if r["plan_nurture"]:        score += 5;  notes.append("+5 plan:nurture")
        if r["plan_partnership_play"]: score -= 10; notes.append("-10 plan:partnership_play")
        if r["plan_let_go"]:         score -= 80; notes.append("-80 plan:let_go (courtesy message pending — lowest priority)")
        if r["plan_pass"]:           score -= 100; notes.append("-100 plan:pass")

        # Active motion
        if r["we_sent_invite"]:      score += 20; notes.append("+20 we:sent_calendar_invite")
        elif r["we_sent_times"]:     score += 15; notes.append("+15 we:sent_them_possible_times")
        if r["we_sent_referral"]:    score -= 10; notes.append("-10 we:sent_referral_ask (routing)")
        if r["we_nudged"]:           score -= 5;  notes.append("-5 we:nudged (already followed up)")

        # Recency of last (inbound for bucket A; outbound for bucket B)
        hrs = float(r["hours_since"] or 0)
        if hrs <= 24:               score += 30; notes.append(f"+30 last_{r['last_dir']} <24h ({hrs}h)")
        elif hrs <= 48:             score += 20; notes.append(f"+20 last_{r['last_dir']} <48h ({hrs}h)")
        elif hrs <= 72:             score += 15; notes.append(f"+15 last_{r['last_dir']} <72h ({hrs}h)")
        elif hrs <= 168:            score += 5;  notes.append(f"+5 last_{r['last_dir']} <7d ({hrs}h)")
        elif hrs <= 336:            score -= 5;  notes.append(f"-5 last_{r['last_dir']} <14d ({hrs}h)")
        else:                       score -= 20; notes.append(f"-20 last_{r['last_dir']} >14d ({hrs}h) — likely ghosted")

        # Geo soft penalty (if out_of_geo flag and not already overridden)
        if r["country"] in OUT_OF_GEO_COUNTRIES:
            score -= 20; notes.append(f"-20 out_of_geo:{r['country']}")

        # Tier ranking for primary sort (lower = higher priority)
        if r["is_senior_ic"]:
            tier = 1
        elif r["is_qualified"]:
            tier = 2
        elif r["is_pending"]:
            tier = 3
        elif r["is_networker"]:
            tier = 4
        elif r["is_disqualified"]:
            tier = 6
        else:
            tier = 5

        # Response-intent rank — PRIMARY ordering within a bucket, by the
        # nature of how the prospect responded (lower = higher priority):
        #   1 ready to meet · 2 asked questions/engaged · 3 polite/positive ·
        #   4 unclassified · 5 declined (always last).
        # Declined is checked first so it ranks last even if they earlier
        # asked a question — the decline is the operative state.
        if r["declined"] or r["not_interested"]:
            intent_rank = 5
        elif (r["accepted_invite"] or r["plan_book_meeting"]
              or r["plan_book_with_eyar"]
              or r["we_sent_invite"] or r["we_sent_times"]):
            intent_rank = 1   # ready to meet (in the meeting flow)
        elif (r["asked_tech_q"] or r["asked_scope"] or r["has_objection"]
              or r["skeptical_engaged"] or r["deep_feedback"]):
            intent_rank = 2   # asked questions / engaged with substance
        elif r["politely_greeted"] or r["willing"]:
            intent_rank = 3   # polite / positive but vague
        else:
            intent_rank = 4   # unclassified — needs a read

        r["bucket"] = bucket
        r["flags"] = flags
        r["book_score"] = score
        r["score_notes"] = notes
        r["tier"] = tier
        r["intent_rank"] = intent_rank
        bucketed.append(r)

    # === Phase 4: filter + sort ===
    wanted_full = {
        "A": "A_awaiting_our_response",
        "B": "B_nudge_eligible",
        "C": "C_snooze_past_due",
        "D": "D_reservoir_past_due",
        "E": "E_just_sent",
        "F": "F_closed_out",
        "G": "G_meeting_booked",
        "H": "H_stale",
        "X": "X_needs_cleanup",
    }
    keep_buckets = {wanted_full[b] for b in wanted_buckets if b in wanted_full}
    filtered = [r for r in bucketed if r["bucket"] in keep_buckets]
    # Sort: action bucket (A>B>C>D>...>X), then RESPONSE-INTENT rank
    # (ready-to-meet > asked-questions > polite > unclassified > declined),
    # then qualification tier (senior_ic > qualified > pending > networker >
    # none > disqualified) as a tiebreaker, then book_score DESC.
    filtered.sort(key=lambda r: (r["bucket"], r["intent_rank"], r["tier"],
                                 -float(r["book_score"])))

    if args.top:
        filtered = filtered[:args.top]

    # === Output ===
    print()
    cur_bucket = None
    for r in filtered:
        if r["bucket"] != cur_bucket:
            cur_bucket = r["bucket"]
            print(f"\n=== {cur_bucket} ({sum(1 for x in filtered if x['bucket']==cur_bucket)}) ===\n")
        qual_label = (
            "★senior_ic" if r["is_senior_ic"]
            else "qualified" if r["is_qualified"]
            else "pending" if r["is_pending"]
            else "networker" if r["is_networker"]
            else "—"
        )
        flag_str = "  ⚑ " + " | ".join(r["flags"]) if r["flags"] else ""
        company = (r["current_company"] or "?")
        title = (r["current_title"] or "?")[:50]
        country = (r["country"] or "?")
        intent_label = {
            1: "intent:ready_to_meet", 2: "intent:asked_questions",
            3: "intent:polite", 4: "intent:UNCLASSIFIED", 5: "intent:declined",
        }[r["intent_rank"]]
        print(f"  [{qual_label:11s}] [{intent_label:22s}] score={r['book_score']:>4.0f}  "
              f"{r['name']:<25s} | {company:<25s} | "
              f"{title:<45s} | {country:<15s} | "
              f"last_{r['last_dir']}={r['last_msg_at']} ({r['hours_since']}h)"
              f"{flag_str}")
        print(f"               url: {r['linkedin_url']}")
        # For UNCLASSIFIED leads (no intent-bearing tag), surface the last inbound
        # message so the agent can READ it and assign a provisional intent rank
        # using the same criteria (ready_to_meet > asked_questions > polite >
        # declined). The script can't infer intent from free text — the agent does.
        if r["intent_rank"] == 4 and r["last_dir"] == "inbound" and r.get("last_body"):
            snippet = " ".join(r["last_body"].split())[:280]
            print(f"               ⟶ READ & RANK — last inbound: \"{snippet}\"")

    print(f"\n--- Total surfaced: {len(filtered)} ---\n")
    n_uncl = sum(1 for r in filtered if r["intent_rank"] == 4 and r["last_dir"] == "inbound")
    if n_uncl:
        print(f"⚠ {n_uncl} UNCLASSIFIED lead(s) shown with their last inbound above. "
              f"Read each and assign a provisional intent rank (ready_to_meet > "
              f"asked_questions > polite > declined), then work them in that order.\n")

    # Upcoming meetings + reminder flags (independent of the reply buckets).
    with connect(args.client) as mconn:
        print_meetings(mconn)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["bucket", "qual", "name", "company", "title", "country",
                        "last_dir", "last_msg_at", "hours_since", "flags",
                        "linkedin_url"])
            for r in filtered:
                qual_label = (
                    "senior_ic" if r["is_senior_ic"]
                    else "qualified" if r["is_qualified"]
                    else "pending" if r["is_pending"]
                    else "networker" if r["is_networker"]
                    else "-"
                )
                w.writerow([r["bucket"], qual_label, r["name"], r["current_company"],
                            r["current_title"], r["country"], r["last_dir"],
                            r["last_msg_at"], r["hours_since"],
                            "|".join(r["flags"]), r["linkedin_url"]])
        print(f"CSV: {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
