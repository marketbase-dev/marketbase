#!/usr/bin/env python3
"""propose-meeting-times

Propose 30-min meeting slots for a Acme prospect with Dana, by reading
Dana's ACTUAL calendar via Unipile (not Calendly).

Rules — see `~/.claude/skills/propose-meeting-times/SKILL.md` for the canonical
spec. Briefly:

  HARD FILTERS (a slot must pass all):
    - Dana allowed: 7:00-23:00 Asia/Jerusalem
    - Prospect allowed: 8:00-18:00 in prospect's local TZ (configurable)
    - >= 24h out from now
    - No conflict with any non-cancelled event
    - No working-weekend (Fri after 1pm IDT + Sat for Dana; Sat-Sun default
      for prospect; Fri-Sat for ME/Gulf prospects)
    - Within proposal horizon (default: this/next week)

  STRONG PREFERENCES (additive score, picks among survivors):
    +50  back-to-back (0-15 min gap) with a ZOOM event
    -INF within 30 min of an IN-PERSON event (effectively a hard exclude)
    +30  fully within prospect's preferred 9-17 local
    +10  prospect-local mid-day (10-15 local)
    +10  convenient for Dana (10-19 IDT — his core focused window)

  SOFT PREFERENCES (tie-breakers):
    +5/+4/+3/+2/+1  earlier-in-week (Mon..Fri)
    +10  Dana-clustering (this day already has >=2 external meetings)
    +5   slot starts on :00 or :30 (which it always will, but kept for clarity)
    -5   edge-of-Dana-day (within 30 min of his first/last event)

  HARD AVOIDS:
    -INF  within 30 min of family/personal events (haircut, kid pickup, etc.)
    -INF  within 2 hours of a flight/travel block
    -10   lunch hour 12-13 IDT (soft penalty, can still book)

Output: top N (default 2) ranked survivors in prospect's local time, with
scoring breakdown for auditability.

Usage:
    python3 propose_meeting_times.py \\
        --client Acme \\
        --prospect-tz Asia/Jakarta \\
        [--prospect-country ID]        # for weekend detection
        [--days-ahead 7]               # default: this/next week
        [--propose 2]
        [--duration-min 30]
        [--show-all]                   # show full ranked list, not just top N
"""
from __future__ import annoacmens

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))


OPERATOR_GOOGLE_ACCOUNT_ID = "TmF2XcaKS3WZL6JXRfY_KQ"
OPERATOR_CALENDAR_ID = "dana@acme.com"
OPERATOR_TZ = ZoneInfo("Asia/Jerusalem")

# ME / Gulf countries with Fri-Sat weekends. Israel works Sun-Thu (handled
# separately for Dana). All others default to Sat-Sun.
FRISAT_WEEKEND_COUNTRIES = {
    "SA", "AE", "OM", "KW", "QA", "BH", "EG", "JO", "LY", "DZ", "YE", "SY",
}


@dataclass
class CalEvent:
    start: datetime           # tz-aware, Asia/Jerusalem
    end: datetime
    title: str
    classification: str       # "zoom" | "in_person" | "personal" | "flight" | "unknown"
    raw: dict = field(repr=False, default_factory=dict)


def classify_event(ev: dict) -> str:
    title = (ev.get("title") or "").lower()
    body = (ev.get("body") or "").lower()
    location = (ev.get("location") or "").lower()

    blob = f"{title} {body} {location}"

    if any(s in blob for s in ("zoom.us", "meet.google.com", "teams.microsoft.com",
                                "webex.com", "whereby.com", "around.co")):
        return "zoom"

    if "✈" in (ev.get("title") or "") or any(
        kw in title for kw in (" flight", "✈️", "→", " to ", "departs", "arrival")
    ):
        if any(code in (ev.get("title") or "") for code in
               ("BOS", "TLV", "DCA", "JFK", "SFO", "LAX", "LHR", "EWR")):
            return "flight"
    if "flight" in title or "✈" in (ev.get("title") or ""):
        return "flight"

    if any(kw in title for kw in (
        "haircut", "תספורת", "אימון", "training", "gym", "ליה", "doctor",
        "dentist", "school", "kid", "family",
    )):
        return "personal"

    # Calendly auto-naming pattern: "X and <Dana Tolts | Roman Labunsky | Eyar Zilberman>"
    if (" and dana tolts" in title or " and bob labunsky" in title
            or " and eyar zilberman" in title):
        return "zoom"

    # Our manual-invite naming convention: "Acme <> <Company> - <Person>"
    if title.startswith("acme <>"):
        return "zoom"

    # Physical-location hints
    if any(kw in location for kw in ("office", "tlv", "tel aviv", "kiryat", "kirya")):
        return "in_person"
    if any(kw in title for kw in ("בקיריה", "party", "shoot", "summit", "conference",
                                   "כנס", "אירוע")):
        return "in_person"

    return "unknown"


def fetch_dana_events(start_utc: datetime, end_utc: datetime) -> list[CalEvent]:
    base = os.environ.get("UNIPILE_BASE_SERVER")
    token = os.environ.get("UNIPILE_ACCESS_TOKEN")
    if not base or not token:
        sys.exit("UNIPILE_BASE_SERVER + UNIPILE_ACCESS_TOKEN must be in env. "
                 "Did you `set -a; source ~/.env.<Client>; set +a`?")

    cal = urllib.parse.quote(OPERATOR_CALENDAR_ID, safe="")
    url = (
        f"https://{base}/api/v1/calendars/{cal}/events"
        f"?account_id={OPERATOR_GOOGLE_ACCOUNT_ID}"
        f"&start={start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&end={end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&expand_recurring=true"
    )
    req = urllib.request.Request(url, headers={"X-API-KEY": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())

    events: list[CalEvent] = []
    for ev in data.get("data", []):
        if ev.get("is_cancelled"):
            continue
        s = ev.get("start") or {}
        e = ev.get("end") or {}
        s_dt_str = s.get("date_time") or (s.get("date") + "T00:00:00Z" if s.get("date") else None)
        e_dt_str = e.get("date_time") or (e.get("date") + "T00:00:00Z" if e.get("date") else None)
        if not s_dt_str or not e_dt_str:
            continue
        try:
            s_dt = datetime.fromisoformat(s_dt_str.replace("Z", "+00:00"))
            e_dt = datetime.fromisoformat(e_dt_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        s_dt = s_dt.astimezone(OPERATOR_TZ)
        e_dt = e_dt.astimezone(OPERATOR_TZ)
        events.append(CalEvent(
            start=s_dt, end=e_dt,
            title=ev.get("title", ""),
            classification=classify_event(ev),
            raw=ev,
        ))
    return events


def weekend_days_for(country: str | None) -> set[int]:
    """Return weekday ints (0=Mon..6=Sun) that are weekend for the prospect."""
    if country and country.upper() in FRISAT_WEEKEND_COUNTRIES:
        return {4, 5}   # Fri, Sat
    return {5, 6}       # Sat, Sun (default)


def is_dana_blocked(slot_jrm: datetime) -> bool:
    """Friday after 13:00 IDT + all Saturday = blocked for Dana."""
    wd = slot_jrm.weekday()
    if wd == 5:  # Saturday
        return True
    if wd == 4 and slot_jrm.hour >= 13:
        return True
    return False


@dataclass
class SlotScore:
    score: float
    notes: list[str]
    excluded: bool = False


def score_slot(slot_jrm: datetime, slot_prospect: datetime,
               events: list[CalEvent],
               prospect_allowed: tuple[int, int],
               prospect_preferred: tuple[int, int],
               duration_min: int) -> SlotScore:
    notes: list[str] = []
    score = 0.0

    slot_end_jrm = slot_jrm + timedelta(minutes=duration_min)

    # === Hard filters ===
    if slot_jrm.hour < 7 or slot_jrm.hour >= 23:
        return SlotScore(0, ["outside Dana allowed (7-23 IDT)"], excluded=True)
    if is_dana_blocked(slot_jrm):
        return SlotScore(0, ["Dana weekend (Fri >=1pm IDT or Sat)"], excluded=True)
    if slot_prospect.hour < prospect_allowed[0] or slot_prospect.hour >= prospect_allowed[1]:
        return SlotScore(0, [f"outside prospect allowed "
                              f"({prospect_allowed[0]:02d}-{prospect_allowed[1]:02d} local)"],
                         excluded=True)

    # Conflict / proximity
    for ev in events:
        # Direct overlap → excluded
        if slot_jrm < ev.end and slot_end_jrm > ev.start:
            return SlotScore(0, [f"conflict with {ev.title!r} ({ev.classification})"],
                             excluded=True)

        gap_before = (slot_jrm - ev.end).total_seconds() / 60.0   # ev ends, gap, slot starts
        gap_after = (ev.start - slot_end_jrm).total_seconds() / 60.0  # slot ends, gap, ev starts

        # Flight: 2-hour buffer
        if ev.classification == "flight":
            if -120 < gap_before < 120 or -120 < gap_after < 120:
                return SlotScore(0, [f"within 2h of flight {ev.title!r}"], excluded=True)

        # Personal / in-person: 30-min buffer
        if ev.classification in ("personal", "in_person"):
            if 0 <= gap_before < 30 or 0 <= gap_after < 30:
                return SlotScore(0, [f"within 30m of {ev.classification} event "
                                      f"{ev.title!r}"],
                                 excluded=True)

        # Unknown: conservative 30-min buffer
        if ev.classification == "unknown":
            if 0 <= gap_before < 30 or 0 <= gap_after < 30:
                return SlotScore(0, [f"within 30m of unknown-type event "
                                      f"{ev.title!r} (treating conservatively)"],
                                 excluded=True)

        # Back-to-back with zoom: bonus
        if ev.classification == "zoom":
            if 0 <= gap_before <= 15:
                score += 50
                notes.append(f"+50 back-to-back after zoom {ev.title!r} "
                             f"(gap={int(gap_before)}m)")
            elif 0 <= gap_after <= 15:
                score += 50
                notes.append(f"+50 back-to-back before zoom {ev.title!r} "
                             f"(gap={int(gap_after)}m)")

    # === Strong preferences ===
    pp_lo, pp_hi = prospect_preferred
    slot_end_prospect = slot_prospect + timedelta(minutes=duration_min)
    if (slot_prospect.hour >= pp_lo and
            slot_end_prospect.hour <= pp_hi and
            not (slot_end_prospect.hour == pp_hi and slot_end_prospect.minute > 0)):
        score += 30
        notes.append(f"+30 within prospect preferred {pp_lo:02d}-{pp_hi:02d} local")

    # Prospect mid-day
    if 10 <= slot_prospect.hour < 15:
        score += 10
        notes.append("+10 prospect mid-day (10-15 local)")

    # Dana convenience window 10-19 IDT
    if 10 <= slot_jrm.hour < 19:
        score += 10
        notes.append("+10 Dana convenience (10-19 IDT)")

    # === Soft preferences ===
    # Earlier in week
    week_bonus = {0: 5, 1: 4, 2: 3, 3: 2, 4: 1}.get(slot_jrm.weekday(), 0)
    if week_bonus:
        score += week_bonus
        notes.append(f"+{week_bonus} earlier-in-week (weekday={slot_jrm.weekday()})")

    # Same-day Dana clustering
    same_day_meetings = sum(
        1 for ev in events
        if ev.start.date() == slot_jrm.date()
           and ev.classification in ("zoom", "in_person", "unknown")
    )
    if same_day_meetings >= 2:
        score += 10
        notes.append(f"+10 Dana clustering (day already has {same_day_meetings} meetings)")

    # On :00 or :30 (always true given our 30-min grid, kept for clarity)
    if slot_jrm.minute in (0, 30):
        score += 5
        notes.append("+5 starts on :00 or :30")

    # Edge-of-day penalty
    same_day = [ev for ev in events if ev.start.date() == slot_jrm.date()]
    if same_day:
        first = min(same_day, key=lambda e: e.start).start
        last = max(same_day, key=lambda e: e.end).end
        if abs((slot_jrm - first).total_seconds()) < 30 * 60:
            score -= 5
            notes.append("-5 edge of Dana's day (near first event)")
        if abs((last - slot_end_jrm).total_seconds()) < 30 * 60:
            score -= 5
            notes.append("-5 edge of Dana's day (near last event)")

    # === Hard avoids (soft) ===
    if slot_jrm.hour == 12:
        score -= 10
        notes.append("-10 Dana lunch hour (12-13 IDT)")

    return SlotScore(score, notes)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--client", required=True,
                    help="Client name (used to source UNIPILE_* env via ~/.env.<Client>). "
                         "If you've already sourced the env, --client is still required "
                         "for skill discoverability but the env is what's actually used.")
    ap.add_argument("--prospect-tz", required=True,
                    help="Prospect's IANA timezone, e.g. America/New_York, Asia/Jakarta.")
    ap.add_argument("--prospect-country",
                    help="Prospect's ISO country code for weekend detection (default Sat-Sun; "
                         "ME/Gulf countries default to Fri-Sat).")
    ap.add_argument("--days-ahead", type=int, default=7,
                    help="Search horizon in days (default 7 = this/next week).")
    ap.add_argument("--propose", type=int, default=2,
                    help="How many top slots to propose (default 2).")
    ap.add_argument("--duration-min", type=int, default=30,
                    help="Slot duration in minutes (default 30).")
    ap.add_argument("--min-hours-out", type=int, default=24,
                    help="Minimum lead time in hours (default 24).")
    ap.add_argument("--prospect-allowed", default="8-18",
                    help="Prospect allowed window 'start-end' in 24h (default 8-18).")
    ap.add_argument("--prospect-preferred", default="9-17",
                    help="Prospect preferred window 'start-end' in 24h (default 9-17).")
    ap.add_argument("--show-all", action="store_true",
                    help="Show full ranked list, not just top N.")
    args = ap.parse_args()

    prospect_tz = ZoneInfo(args.prospect_tz)
    p_allowed = tuple(int(x) for x in args.prospect_allowed.split("-"))
    p_preferred = tuple(int(x) for x in args.prospect_preferred.split("-"))
    prospect_weekend = weekend_days_for(args.prospect_country)

    # Time window
    now_utc = datetime.now(tz=timezone.utc)
    min_start_utc = now_utc + timedelta(hours=args.min_hours_out)
    end_utc = now_utc + timedelta(days=args.days_ahead)

    events = fetch_dana_events(now_utc, end_utc)
    print(f"Fetched {len(events)} Dana events in next {args.days_ahead} days.",
          file=sys.stderr)

    # Build candidate slots, in 30-min increments aligned to :00/:30, in IDT
    candidates: list[tuple[datetime, datetime]] = []
    cursor = min_start_utc.astimezone(OPERATOR_TZ)
    # Round up to next :00 or :30
    minute_offset = (30 - cursor.minute % 30) % 30
    cursor = cursor.replace(second=0, microsecond=0) + timedelta(minutes=minute_offset)
    end_jrm = end_utc.astimezone(OPERATOR_TZ)

    while cursor < end_jrm:
        slot_prospect = cursor.astimezone(prospect_tz)
        # Skip prospect-weekend
        if slot_prospect.weekday() not in prospect_weekend:
            candidates.append((cursor, slot_prospect))
        cursor += timedelta(minutes=30)

    print(f"Built {len(candidates)} candidate 30-min slots after prospect-weekend "
          f"filter.", file=sys.stderr)

    # Score
    scored: list[tuple[datetime, datetime, SlotScore]] = []
    for slot_jrm, slot_p in candidates:
        sc = score_slot(slot_jrm, slot_p, events, p_allowed, p_preferred,
                        args.duration_min)
        if not sc.excluded:
            scored.append((slot_jrm, slot_p, sc))

    scored.sort(key=lambda x: -x[2].score)

    if not scored:
        print("\nNO viable slots found in horizon. Consider widening --days-ahead "
              "or relaxing windows.")
        return 1

    show_n = len(scored) if args.show_all else args.propose
    print(f"\n=== Top {show_n} of {len(scored)} viable slots ===\n")

    p_tz_label = args.prospect_tz.split("/")[-1].upper()
    for i, (sj, sp, sc) in enumerate(scored[:show_n], 1):
        prospect_str = sp.strftime(f"%a %b %d, %-I:%M %p {p_tz_label}")
        dana_str = sj.strftime("%-I:%M %p IDT")
        print(f"  {i}. {prospect_str}  (= {dana_str})")
        print(f"     Score: {sc.score:.0f}")
        for note in sc.notes:
            print(f"       {note}")
        print()

    print(f"PROPOSE THESE {args.propose}:")
    for sj, sp, sc in scored[:args.propose]:
        prospect_str = sp.strftime(f"%a %b %d, %-I:%M %p {p_tz_label}")
        print(f"  - {prospect_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
