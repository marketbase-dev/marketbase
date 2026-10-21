#!/usr/bin/env python3
"""ingest_luma_guests — pull a Luma event's guest list (admin API) into marketbase.

For each guest: upsert a lead keyed on the email identity (lead_identities,
schema 039 — most Luma guests have no LinkedIn URL), append a `lead_sources`
provenance row carrying the raw guest object (approval status, check-in,
registration answers, utm, geo), and apply an audience tag.

Every API page is cached read-through/write-through in `enrichment_calls`
via api_cache (api='luma', endpoint='/event/admin/get-guests'), so a re-run
replays pages from the database instead of re-fetching.

Attendance status is point-in-time provenance, NOT mutable lead state: each
run appends a new lead_sources row only when the guest's status changed since
the last recorded row. Current status = latest row per lead for this event.

Auth: the Luma session cookie value is read from the env var named by
--cookie-env (default LUMA_SESSION_COOKIE) — never from argv, never from disk.
Grab it from the logged-in browser profile:
  node ~/.claude/tools/browser-operate/browser_operation/cookies.js \
       --profile "<name>" --domain luma.com --names "luma.auth-session-key"

Usage:
  LUMA_SESSION_COOKIE=... python3 ingest_luma_guests.py \
      --client "Northwind" \
      --event-id evt-XXXXXXXXXXXXXXX \
      --source-label "Community Meetup (Luma evt-XXXXXXXXXXXXXXX)" \
      --tag audience:meetup_applicant
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, normalize_linkedin_url  # noqa: E402
import api_cache  # noqa: E402

TOOL = "ingest_luma_guests.py"
API = "luma"
ENDPOINT = "/event/admin/get-guests"
BASE = "https://api.luma.com"
PAGE_LIMIT = 100

SOURCE_TYPE = "luma_event_guest"
SOURCE_TYPE_ROW = (
    SOURCE_TYPE,
    "Guest of a Luma event we administer (registration/application via luma.com).",
    "Use when ingesting a Luma event's guest list via the admin API "
    "(ingest_luma_guests.py). One lead_sources row per guest per status change: "
    "the row is a point-in-time snapshot, current status = latest row per lead "
    "for the event named in source_label.",
    "Raw event_guest object from api.luma.com /event/admin/get-guests: name, "
    "email, approval_status (approved|pending_approval|invited|declined|"
    "waitlist), registered_at, invited_at, checked_in_at, geo_city, "
    "geo_country, utm_source, linkedin_handle, registration_answers, "
    "event_tickets; plus _source naming this tool.",
    TOOL,
)


def luma_handle_to_url(handle: str | None) -> str | None:
    if not handle:
        return None
    h = handle.strip()
    if not h:
        return None
    if h.startswith("http://") or h.startswith("https://"):
        return normalize_linkedin_url(h)
    h = h.lstrip("/")
    if not h.startswith("in/") and not h.startswith("company/"):
        h = "in/" + h
    return normalize_linkedin_url(f"https://www.linkedin.com/{h}")


def fetch_page(cookie: str, event_id: str, cursor: str | None):
    params = {"event_api_id": event_id, "pagination_limit": PAGE_LIMIT}
    if cursor:
        params["pagination_cursor"] = cursor
    r = requests.get(
        BASE + ENDPOINT,
        params=params,
        headers={
            "Cookie": f"luma.auth-session-key={cookie}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def page_params(event_id: str, cursor: str | None) -> dict:
    # Cache key per page — cookie deliberately excluded.
    p = {"event_api_id": event_id, "pagination_limit": PAGE_LIMIT}
    if cursor:
        p["pagination_cursor"] = cursor
    return p


def is_guest_page(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("entries"), list)


def ensure_registries(cur, tag: str):
    cur.execute(
        """
        INSERT INTO source_types (name, description, purpose, raw_data_shape, created_by)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (name) DO NOTHING
        """,
        SOURCE_TYPE_ROW,
    )
    ns, _, key = tag.partition(":")
    cur.execute(
        """
        INSERT INTO tag_definitions (namespace, key, entity_type, layer, description)
        VALUES (%s, %s, 'lead', 'audience', %s)
        ON CONFLICT (namespace, key, entity_type) DO NOTHING
        """,
        (
            ns,
            key,
            "Registered/applied as a guest of a Luma event we administer (see "
            "luma_event_guest lead_sources rows for which event and the "
            "status at each ingest).",
        ),
    )


def find_lead(cur, email: str | None, li_url: str | None):
    if email:
        cur.execute(
            "SELECT lead_id FROM lead_identities WHERE kind='email' AND value=%s",
            (email,),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("SELECT id FROM leads WHERE lower(email)=%s", (email,))
        row = cur.fetchone()
        if row:
            return row[0]
    if li_url:
        cur.execute(
            "SELECT lead_id FROM lead_identities WHERE kind='linkedin_url' AND value=%s",
            (li_url,),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("SELECT id FROM leads WHERE linkedin_url=%s", (li_url,))
        row = cur.fetchone()
        if row:
            return row[0]
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client", default="Impact 11")
    ap.add_argument("--event-id", required=True)
    ap.add_argument("--source-label", required=True)
    ap.add_argument("--tag", required=True,
                    help="lead_tags tag, namespace:key (e.g. audience:meetup_applicant)")
    ap.add_argument("--cookie-env", default="LUMA_SESSION_COOKIE")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the enrichment_calls read path (still writes it)")
    args = ap.parse_args()

    cookie = os.environ.get(args.cookie_env, "").strip()
    if not cookie:
        sys.exit(f"Set {args.cookie_env} to the luma.auth-session-key cookie value.")
    if ":" not in args.tag:
        sys.exit("--tag must be namespace:key (e.g. audience:meetup_applicant)")

    conn = connect(args.client)
    guests, cursor, pages = [], None, 0
    try:
        with conn.cursor() as cur:
            ensure_registries(cur, args.tag)

            while True:
                data, cached = api_cache.cached_call(
                    conn, API, ENDPOINT, page_params(args.event_id, cursor),
                    lambda c=cursor: fetch_page(cookie, args.event_id, c),
                    use_cache=not args.no_cache, cost=0,
                    success_fn=is_guest_page,
                )
                if not is_guest_page(data):
                    raise RuntimeError(f"Unexpected page shape: {str(data)[:300]}")
                pages += 1
                guests.extend(data["entries"])
                print(f"page {pages}: {len(data['entries'])} guests"
                      f" ({'cache' if cached else 'fetched'})")
                cursor = data.get("next_cursor")
                if not data.get("has_more") or not cursor:
                    break

            created = matched = sources_added = sources_skipped = tags_added = 0
            by_status: dict[str, int] = {}

            for g in guests:
                email = (g.get("email") or "").strip().lower() or None
                li_url = luma_handle_to_url(g.get("linkedin_handle"))
                status = g.get("approval_status") or "unknown"
                by_status[status] = by_status.get(status, 0) + 1
                if not email and not li_url:
                    print(f"  !! no identity for guest {g.get('api_id')} "
                          f"({g.get('name')}); skipped")
                    continue

                lead_id = find_lead(cur, email, li_url)
                if lead_id is None:
                    cur.execute(
                        """
                        INSERT INTO leads (linkedin_url, name, email, city, country)
                        VALUES (%s, %s, %s, %s, %s) RETURNING id
                        """,
                        (li_url, g.get("name"), email,
                         g.get("geo_city"), g.get("geo_country")),
                    )
                    lead_id = cur.fetchone()[0]
                    created += 1
                else:
                    matched += 1
                    # fill blanks only, keyed by id
                    cur.execute(
                        """
                        UPDATE leads SET
                          name    = COALESCE(name, %s),
                          email   = COALESCE(email, %s),
                          city    = COALESCE(city, %s),
                          country = COALESCE(country, %s),
                          updated_at = now()
                        WHERE id = %s
                        """,
                        (g.get("name"), email, g.get("geo_city"),
                         g.get("geo_country"), lead_id),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"expected 1 row updating lead {lead_id}, "
                                           f"got {cur.rowcount}")

                identity_src = f"luma:{args.event_id}"
                if email:
                    cur.execute(
                        """
                        INSERT INTO lead_identities (lead_id, kind, value, source)
                        VALUES (%s, 'email', %s, %s)
                        ON CONFLICT (kind, value) DO NOTHING
                        """,
                        (lead_id, email, identity_src),
                    )
                if li_url:
                    cur.execute(
                        """
                        INSERT INTO lead_identities (lead_id, kind, value, source)
                        VALUES (%s, 'linkedin_url', %s, %s)
                        ON CONFLICT (kind, value) DO NOTHING
                        """,
                        (lead_id, li_url, identity_src),
                    )

                # Append a provenance row only when this guest's status changed
                # since the last recorded row for this event.
                cur.execute(
                    """
                    SELECT raw_data->>'approval_status',
                           raw_data->>'checked_in_at'
                      FROM lead_sources
                     WHERE lead_id = %s AND source_type = %s
                       AND raw_data->>'api_id' = %s
                     ORDER BY recorded_at DESC LIMIT 1
                    """,
                    (lead_id, SOURCE_TYPE, g.get("api_id")),
                )
                prev = cur.fetchone()
                cur_checkin = g.get("checked_in_at")
                if prev and prev[0] == status and prev[1] == cur_checkin:
                    sources_skipped += 1
                else:
                    raw = dict(g)
                    raw["_source"] = TOOL
                    cur.execute(
                        """
                        INSERT INTO lead_sources
                          (lead_id, source_type, source_label, source_date, raw_data)
                        VALUES (%s, %s, %s, CURRENT_DATE, %s::jsonb)
                        """,
                        (lead_id, SOURCE_TYPE, args.source_label,
                         json.dumps(raw, default=str)),
                    )
                    sources_added += 1

                cur.execute(
                    """
                    INSERT INTO lead_tags (lead_id, tag, tagged_by)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (lead_id, tag) DO NOTHING
                    """,
                    (lead_id, args.tag, TOOL),
                )
                tags_added += cur.rowcount

        conn.commit()
        print(f"\n{len(guests)} guests across {pages} page(s)")
        print(f"  leads created: {created}, matched existing: {matched}")
        print(f"  lead_sources rows added: {sources_added} "
              f"(unchanged, skipped: {sources_skipped})")
        print(f"  tags applied: {tags_added} ({args.tag})")
        print("  by approval_status: "
              + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
