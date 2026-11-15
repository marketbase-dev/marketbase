#!/usr/bin/env python3
"""find_speakers.py — speakers at conferences and podcasts, read off public pages (schema 097).

A conference series or a podcast show is a `community`, each edition or episode an `event`, and
a speaker, host or guest a membership with that role (096). This tool files them. Searching the
web is done by the session that runs it (its own search, or subagents): the tool says what to
search, stores what came back, reads the pages itself and checks every speaker against the page
before writing. Everything fetched lands in enrichment_calls; nothing is written to disk.

  policy          show the active search rules per topic set
  set-policy      --topic-set T --terms "a,b,c" [--months-back 12 --months-ahead 6] --source S
                  --rationale R: a new version; the old one is marked superseded
  plan            the searches to run for each topic set and its window, and which are still
                  fresh in the cache
  record-search   stdin: {"query", "topic_set", "results": [...]}: what a search returned. Stored
                  as a cached call; it creates no event
  add-event       --name N --url U [--kind conference|podcast_episode|meetup|webinar]
                  [--speakers-url U] [--starts D --ends D] [--location L] [--edition 2026]
                  [--series-name N --series-url U] [--organiser O] [--topic-set T ...]
                  reads the event page (cached), then files the event and its series
  page            --url U: read a page (cached) and print its text, for whoever extracts speakers
  extract         --event ID: read the event's speaker page and print the speakers the tool can
                  read by itself (a Sessionize feed, or structured data in the page), as JSON
  record-speakers --event ID [--apply], stdin: [{"name", "title", "company", "role",
                  "linkedin_url", "entry_url", "platform_id", "talks": [{"title", "url"}]}]
                  a speaker whose name is not on a cached page of that event is refused
  report          events, speakers, and how many carry a profile URL

A speaker with no profile URL is keyed by their own entry on the event site (identity kind
event_speaker_page), never by name. Without --apply, record-speakers only reports.

Usage:
  python3 find_speakers.py plan --client <Name>
  python3 find_speakers.py add-event --client <Name> --name "Northwind Summit 2026" --url https://...
  python3 find_speakers.py extract --client <Name> --event <id> | \\
      python3 find_speakers.py record-speakers --client <Name> --event <id> --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmllib
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, lead_id_for_linkedin_url, live_lead_id, normalize_linkedin_url  # noqa: E402
import api_cache  # noqa: E402

ME = "find_speakers.py"
SOURCE_TYPE = "event_speaker"
KIND = "event_speaker_page"
ROLES = {"speaker", "host", "guest"}
SUBTYPES = ["conference", "podcast_episode", "meetup", "webinar"]
UA = "Mozilla/5.0 (compatible; event-page-reader)"
SEARCH_SHAPES = ['{term} conference {year}', '{term} summit {year}', '{term} event speakers {year}',
                 '{term} podcast', '{term} podcast guests']
THIN = 800          # a page with less visible text than this is tried again through the reader


# ── text helpers ───────────────────────────────────────────────────────────────
def norm_url(u: str) -> str:
    s = urlsplit(u.strip())
    q = urlencode([(k, v) for k, v in parse_qsl(s.query) if not k.lower().startswith("utm_")])
    return urlunsplit((s.scheme.lower() or "https", s.netloc.lower(), s.path.rstrip("/") or "", q, ""))


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def slug(s: str) -> str:
    return re.sub(r"\s+", "-", fold(s))


def visible_text(page_html: str) -> str:
    t = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", page_html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmllib.unescape(t)).strip()


# ── reading pages (free only: the site itself, then a free reader) ─────────────
def _fetch_page(url: str) -> dict | None:
    best = None
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        if r.status_code == 200 and r.text:
            best = {"url": url, "via": "direct", "status": 200, "html": r.text, "text": visible_text(r.text)}
    except requests.RequestException:
        pass
    if best is None or len(best["text"]) < THIN:
        try:
            r = requests.get("https://r.jina.ai/" + url, headers={"Accept": "text/plain"}, timeout=60)
            if r.status_code == 200 and len(r.text) > len((best or {}).get("text", "")):
                reader = {"url": url, "via": "reader", "status": 200, "text": r.text}
                if best:
                    reader["html"] = best["html"]      # keep the markup: structured data lives there
                best = reader
        except requests.RequestException:
            pass
    return best


def read_page(conn, url: str, refresh: bool = False) -> dict:
    url = norm_url(url)
    data, cached = api_cache.cached_call(conn, "web", "page", {"url": url}, lambda: _fetch_page(url),
                                         use_cache=not refresh, cost=0)
    if not data:
        sys.exit(f"could not read {url} from the site or the free reader. A paid reader is not wired in: "
                 f"it needs a costed plan first.")
    print(f"  page {url}: {len(data.get('text', ''))} chars via {data.get('via')}"
          f" ({'cache' if cached else 'fetched'})", file=sys.stderr)
    return data


def cached_pages(cur, urls: list[str]) -> list[dict]:
    """Every cached success for these pages, whatever its age: the check is "was it ever on the page"."""
    cur.execute("""SELECT response FROM enrichment_calls
                    WHERE api = 'web' AND endpoint = 'page' AND success AND params->>'url' = ANY(%s)""",
                ([norm_url(u) for u in urls if u],))
    return [r[0] for r in cur.fetchall()]


# ── speakers the tool can read by itself ───────────────────────────────────────
def sessionize_speakers(conn, page: dict) -> list[dict]:
    m = re.search(r"sessionize\.com/api/v2/([a-z0-9]{6,12})", page.get("html") or page.get("text") or "")
    if not m:
        return []
    eid = m.group(1)

    def fetch():
        r = requests.get(f"https://sessionize.com/api/v2/{eid}/view/Speakers", headers={"User-Agent": UA}, timeout=30)
        return r.json() if r.status_code == 200 else None

    data, _ = api_cache.cached_call(conn, "sessionize", "/api/v2/{id}/view/Speakers", {"id": eid}, fetch,
                                    cost=0, success_fn=lambda d: isinstance(d, list))
    out = []
    for s in data or []:
        link = next((x.get("url") for x in s.get("links") or [] if "linkedin" in str(x.get("linkType", "")).lower()), None)
        out.append({"name": s.get("fullName"), "title": s.get("tagLine"), "company": None, "role": "speaker",
                    "linkedin_url": link, "platform_id": f"sessionize:{s.get('id')}", "bio": s.get("bio"),
                    "talks": [{"title": x.get("name")} for x in s.get("sessions") or []]})
    return out


def jsonld_speakers(page: dict) -> list[dict]:
    out = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            for key in ("performer", "performers", "speaker", "speakers", "actor"):
                people = node.get(key)
                for p in (people if isinstance(people, list) else [people]) if people else []:
                    if isinstance(p, dict) and p.get("name"):
                        org = p.get("worksFor") or p.get("affiliation") or {}
                        same = p.get("sameAs") or []
                        same = same if isinstance(same, list) else [same]
                        out.append({"name": p["name"], "title": p.get("jobTitle"),
                                    "company": org.get("name") if isinstance(org, dict) else org,
                                    "role": "speaker", "entry_url": p.get("url"),
                                    "linkedin_url": next((u for u in same if "linkedin.com/in/" in str(u)), None)})
            for v in node.values():
                walk(v)

    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page.get("html") or ""):
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return out


# ── events ─────────────────────────────────────────────────────────────────────
def upsert_collective(cur, kind, name, provider_id, url, raw, starts=None, ends=None, location=None, parent=None) -> str:
    cur.execute("""
        INSERT INTO collectives (kind, name, provider, provider_id, url, starts_at, ends_at, location, parent_id,
                                 parent_source, source, raw)
        VALUES (%s, %s, 'web', %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (provider, provider_id) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, collectives.name), url = COALESCE(EXCLUDED.url, collectives.url),
            starts_at = COALESCE(EXCLUDED.starts_at, collectives.starts_at),
            ends_at = COALESCE(EXCLUDED.ends_at, collectives.ends_at),
            location = COALESCE(EXCLUDED.location, collectives.location),
            parent_id = COALESCE(collectives.parent_id, EXCLUDED.parent_id),
            parent_source = COALESCE(collectives.parent_source, EXCLUDED.parent_source),
            raw = COALESCE(collectives.raw, '{}'::jsonb) || EXCLUDED.raw, last_seen_at = now()
        RETURNING id::text""",
                (kind, name, provider_id, url, starts, ends, location, parent, "inferred" if parent else None,
                 ME, json.dumps(raw)))
    return cur.fetchone()[0]


def stage_add_event(conn, a) -> int:
    page = read_page(conn, a.url)
    if fold(a.name) not in fold(page.get("text", "")):
        words = [w for w in fold(a.name).split() if len(w) > 3 and not w.isdigit()]
        if not words or not all(w in fold(page["text"]) for w in words[:2]):
            sys.exit(f"the event page does not mention {a.name!r}; refusing to file an event the page does not show")
        print(f"  ! exact name {a.name!r} is not on the page, but its main words are", file=sys.stderr)
    cur = conn.cursor()
    parent = None
    if a.series_url:
        parent = upsert_collective(cur, "community", a.series_name or a.name, norm_url(a.series_url),
                                   norm_url(a.series_url), {"subtype": "series", "organiser": a.organiser})
    pid = norm_url(a.url) + (f"#{a.edition}" if a.edition else "")
    raw = {"subtype": a.kind, "edition": a.edition, "organiser": a.organiser, "topic_sets": sorted(set(a.topic_set or [])),
           "speakers_url": norm_url(a.speakers_url) if a.speakers_url else None, "page_read_via": page.get("via")}
    eid = upsert_collective(cur, "event", a.name, pid, norm_url(a.url), {k: v for k, v in raw.items() if v},
                            a.starts, a.ends, a.location, parent)
    conn.commit()
    print(json.dumps({"event_id": eid, "series_id": parent, "provider_id": pid}))
    return 0


def event_row(cur, event_id: str):
    cur.execute("SELECT id::text, name, url, raw FROM collectives WHERE id::text = %s AND kind = 'event' AND provider = 'web'",
                (event_id,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"no web event with id {event_id}")
    return row


def stage_page(conn, a) -> int:
    print(read_page(conn, a.url, a.refresh).get("text", ""))
    return 0


def stage_extract(conn, a) -> int:
    cur = conn.cursor()
    _, name, url, raw = event_row(cur, a.event)
    page = read_page(conn, (raw or {}).get("speakers_url") or url, a.refresh)
    found = sessionize_speakers(conn, page) or jsonld_speakers(page)
    print(f"  {name}: {len(found)} speakers readable without help"
          + ("" if found else "; read the page text (stage `page`) and pass the speakers to record-speakers"),
          file=sys.stderr)
    print(json.dumps(found, indent=1))
    return 0


# ── speakers ───────────────────────────────────────────────────────────────────
def stage_record_speakers(conn, a) -> int:
    cur = conn.cursor()
    eid, ename, eurl, raw = event_row(cur, a.event)
    speakers_url = (raw or {}).get("speakers_url") or eurl
    people = json.load(sys.stdin)
    if not isinstance(people, list):
        sys.exit("stdin must be a JSON list of speakers")
    pages = cached_pages(cur, [eurl, speakers_url] + list(a.evidence_url or []))
    hay = " ".join(fold(p.get("text", "")) for p in pages)
    raw_hay = " ".join((p.get("html") or "") + (p.get("text") or "") for p in pages).lower()
    # a speaker feed counts only when one of this event's own pages embeds it
    feeds = sorted(set(re.findall(r"sessionize\.com/api/v2/([a-z0-9]{6,12})", raw_hay)))
    cur.execute("""SELECT response FROM enrichment_calls
                    WHERE api = 'sessionize' AND success AND params->>'id' = ANY(%s)""", (feeds,))
    feed_names = {fold(s.get("fullName", "")) for r in cur.fetchall() for s in (r[0] or []) if isinstance(s, dict)}

    accepted, refused = [], []
    for p in people:
        name = (p.get("name") or "").strip()
        role = p.get("role") or "speaker"
        if not name or role not in ROLES:
            refused.append((name or "(no name)", f"role {role!r}" if name else "no name"))
        elif fold(name) not in hay and fold(name) not in feed_names:
            refused.append((name, "name is not on a cached page of this event"))
        else:
            li = normalize_linkedin_url(p.get("linkedin_url")) if p.get("linkedin_url") else None
            if li and li.rstrip("/").rsplit("/", 1)[-1].lower() not in raw_hay:
                refused.append((name, "profile URL is not on a cached page of this event"))
                continue
            entry = norm_url(p["entry_url"]) if p.get("entry_url") else f"{norm_url(speakers_url)}#{p.get('platform_id') or slug(name)}"
            accepted.append(dict(p, name=name, role=role, linkedin_url=li, entry=entry))
    print(f"{ename}: {len(people)} given, {len(accepted)} on the page, {len(refused)} refused")
    for name, why in refused:
        print(f"  refused: {name}: {why}")
    if not a.apply:
        print("plan only; pass --apply to write")
        return 0 if not refused else 1
    if not accepted:
        return 1

    cur.execute("""INSERT INTO source_types (name, description, purpose, created_by)
                   VALUES (%s, 'A person listed as a speaker, host or guest on the public page of a conference, '
                           'podcast episode or other event (097).',
                           'People who speak in public on a topic, with the event and the talk.', %s)
                   ON CONFLICT (name) DO NOTHING""", (SOURCE_TYPE, ME))
    cur.execute("SELECT lead_id::text, member_kind, member_value FROM v_collective_members WHERE collective_id::text = %s "
                "AND lead_id IS NOT NULL", (eid,))
    known = {lead: (k, v) for lead, k, v in cur.fetchall()}
    n = dict(created=0, matched=0, sources=0, memberships=0, with_profile=0)
    for p in accepted:
        lead = live_lead_id(cur, lead_id_for_linkedin_url(cur, p["linkedin_url"])) if p["linkedin_url"] else None
        if lead is None:
            cur.execute("SELECT lead_id::text FROM lead_identities WHERE kind = %s AND value = %s", (KIND, p["entry"]))
            row = cur.fetchone()
            lead = live_lead_id(cur, row[0]) if row else None
        if lead is None:
            cur.execute("INSERT INTO leads (name, linkedin_url) VALUES (%s, %s) RETURNING id::text", (p["name"], p["linkedin_url"]))
            lead = cur.fetchone()[0]
            n["created"] += 1
        else:
            lead = str(lead)
            n["matched"] += 1
        for kind, value in ((KIND, p["entry"]), ("linkedin_url", p["linkedin_url"])):
            if value:
                cur.execute("SELECT lead_identity_claim(%s::uuid, %s, %s, %s, false)", (lead, kind, value, f"{ME} {eid}"))
        n["with_profile"] += bool(p["linkedin_url"])
        stated = {"title": p.get("title"), "company": p.get("company"), "talks": p.get("talks") or [],
                  "bio": p.get("bio"), "page": norm_url(speakers_url), "as_stated_on": dt.date.today().isoformat()}
        stated = {k: v for k, v in stated.items() if v}
        cur.execute("SELECT 1 FROM lead_sources WHERE lead_id::text = %s AND source_type = %s AND raw_data->>'collective_id' = %s",
                    (lead, SOURCE_TYPE, eid))
        if not cur.fetchone():
            cur.execute("""INSERT INTO lead_sources (lead_id, source_type, source_label, source_date, raw_data)
                           VALUES (%s::uuid, %s, %s, current_date, %s::jsonb)""",
                        (lead, SOURCE_TYPE, f"{ename} ({p['role']})",
                         json.dumps(dict(stated, _source=ME, collective_id=eid, role=p["role"]))))
            n["sources"] += 1
        kind, value = known.get(lead) or (("linkedin_url", p["linkedin_url"]) if p["linkedin_url"] else (KIND, p["entry"]))
        cur.execute("""
            INSERT INTO collective_memberships (collective_id, member_kind, member_value, role, source, source_ref, raw)
            VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (collective_id, member_kind, member_value) DO UPDATE SET
                role = EXCLUDED.role, source = EXCLUDED.source, source_ref = EXCLUDED.source_ref,
                raw = EXCLUDED.raw, last_seen_at = now()""",
                    (eid, kind, value, p["role"], ME, norm_url(speakers_url), json.dumps(stated)))
        n["memberships"] += cur.rowcount
        known[lead] = (kind, value)
    cur.execute("SELECT count(*) FROM collective_memberships WHERE collective_id::text = %s AND source = %s", (eid, ME))
    on_file = cur.fetchone()[0]
    if n["memberships"] != len(accepted) or on_file < len({p["entry"] for p in accepted}):
        conn.rollback()
        sys.exit(f"expected {len(accepted)} memberships, wrote {n['memberships']} ({on_file} on file); rolled back")
    conn.commit()
    print("written:", n)
    return 0 if not refused else 1


# ── search rules and the searches to run ───────────────────────────────────────
def active_policies(cur) -> list[tuple]:
    cur.execute("""SELECT topic_set, version, search_terms, months_back, months_ahead, source
                     FROM speaker_search_policy WHERE superseded_at IS NULL ORDER BY topic_set""")
    return cur.fetchall()


def stage_policy(conn, a) -> int:
    rows = active_policies(conn.cursor())
    for t, v, terms, back, ahead, src in rows:
        print(f"{t} @ {v}: {back} months back, {ahead} ahead; source: {src}\n  terms: {', '.join(terms)}")
    if not rows:
        print("no search rules yet; add them with set-policy")
    return 0


def stage_set_policy(conn, a) -> int:
    terms = [t.strip() for t in a.terms.split(",") if t.strip()]
    cur = conn.cursor()
    cur.execute("SELECT version FROM speaker_search_policy WHERE topic_set = %s", (a.topic_set,))
    taken = {r[0] for r in cur.fetchall()}
    version = base = dt.date.today().isoformat()
    i = 1
    while version in taken:
        i += 1
        version = f"{base}.{i}"
    cur.execute("""UPDATE speaker_search_policy SET superseded_at = now()
                    WHERE topic_set = %s AND superseded_at IS NULL""", (a.topic_set,))
    if cur.rowcount > 1:
        conn.rollback()
        sys.exit(f"expected at most 1 active row for {a.topic_set}, found {cur.rowcount}")
    cur.execute("""INSERT INTO speaker_search_policy (topic_set, version, search_terms, months_back, months_ahead,
                                                      source, rationale, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (a.topic_set, version, terms, a.months_back, a.months_ahead, a.source, a.rationale, ME))
    conn.commit()
    print(f"{a.topic_set} @ {version}: {len(terms)} terms, {a.months_back} months back, {a.months_ahead} ahead")
    return 0


def search_params(query, topic_set, back, ahead) -> dict:
    return {"query": query, "topic_set": topic_set, "months_back": back, "months_ahead": ahead}


def stage_plan(conn, a) -> int:
    cur = conn.cursor()
    today = dt.date.today()
    todo = 0
    for t, v, terms, back, ahead, _ in active_policies(cur):
        start = today - dt.timedelta(days=30 * back)
        end = today + dt.timedelta(days=30 * ahead)
        years = sorted({start.year, today.year, end.year})
        print(f"\n{t} @ {v}: events and episodes dated {start} to {end}")
        for term in terms:
            for shape in SEARCH_SHAPES:
                for q in sorted({shape.format(term=term, year=y) for y in years}):
                    days = api_cache.resolve_fresh_days(conn, "agent_search", "event_discovery")
                    hit = api_cache.get(conn, "agent_search", "event_discovery", search_params(q, t, back, ahead),
                                        max_age_days=days)
                    todo += hit is None
                    print(f"  [{'cached' if hit is not None else 'search'}] {q}")
    print(f"\n{todo} searches to run. Each one: search the web, then pass what it found to record-search.")
    return 0


def stage_record_search(conn, a) -> int:
    doc = json.load(sys.stdin)
    cur = conn.cursor()
    pol = {r[0]: r for r in active_policies(cur)}
    if doc.get("topic_set") not in pol or not doc.get("query") or not isinstance(doc.get("results"), list):
        sys.exit('stdin must be {"query", "topic_set" (one with active rules), "results": [...]}')
    _, _, _, back, ahead, _ = pol[doc["topic_set"]]
    api_cache.put(conn, "agent_search", "event_discovery", search_params(doc["query"], doc["topic_set"], back, ahead),
                  True, {"results": doc["results"], "searched_by": doc.get("searched_by"), "_source": ME}, cost=0)
    print(f"stored {len(doc['results'])} results for {doc['query']!r}")
    return 0


def stage_report(conn, a) -> int:
    cur = conn.cursor()
    cur.execute("""SELECT c.name, c.raw->>'subtype', c.starts_at::date, count(m.id),
                          count(*) FILTER (WHERE m.member_kind = 'linkedin_url'), c.id::text
                     FROM collectives c LEFT JOIN collective_memberships m ON m.collective_id = c.id AND m.source = %s
                    WHERE c.kind = 'event' AND c.provider = 'web' GROUP BY c.id ORDER BY c.starts_at NULLS LAST, c.name""", (ME,))
    print("event, type, starts, speakers, keyed by profile URL, id")
    for r in cur.fetchall():
        print("  ", r)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["policy", "set-policy", "plan", "record-search", "add-event", "page", "extract",
                                      "record-speakers", "report"])
    ap.add_argument("--client", required=True)
    ap.add_argument("--topic-set", action="append")
    ap.add_argument("--terms")
    ap.add_argument("--months-back", type=int, default=12)
    ap.add_argument("--months-ahead", type=int, default=6)
    ap.add_argument("--source")
    ap.add_argument("--rationale")
    ap.add_argument("--name")
    ap.add_argument("--url")
    ap.add_argument("--kind", default="conference", choices=SUBTYPES)
    ap.add_argument("--speakers-url")
    ap.add_argument("--starts")
    ap.add_argument("--ends")
    ap.add_argument("--location")
    ap.add_argument("--edition")
    ap.add_argument("--series-name")
    ap.add_argument("--series-url")
    ap.add_argument("--organiser")
    ap.add_argument("--event")
    ap.add_argument("--evidence-url", action="append", help="another cached page of the event that names speakers")
    ap.add_argument("--refresh", action="store_true", help="read the page again even if the cache is fresh")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    need = {"set-policy": ["topic_set", "terms", "source", "rationale"], "add-event": ["name", "url"],
            "page": ["url"], "extract": ["event"], "record-speakers": ["event"]}.get(a.stage, [])
    missing = [f"--{k.replace('_', '-')}" for k in need if not getattr(a, k)]
    if missing:
        sys.exit(f"{a.stage} needs {', '.join(missing)}")
    if a.stage == "set-policy":
        a.topic_set = a.topic_set[0]
    conn = connect(a.client)
    try:
        return {"policy": stage_policy, "set-policy": stage_set_policy, "plan": stage_plan,
                "record-search": stage_record_search, "add-event": stage_add_event, "page": stage_page,
                "extract": stage_extract, "record-speakers": stage_record_speakers,
                "report": stage_report}[a.stage](conn, a)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
