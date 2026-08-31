#!/usr/bin/env python3
"""End-to-end orchestrator for the marketbase-engager-persona-report skill.

Pipeline:
  1. Fetch posts + engagers for each thought leader (per-leader subprocess
     for connection resilience).
  2. Identify engagers with >= min-engagements engagements with the leader-
     set's ORIGINAL posts, tag them `engager:<report-name>`.
  3. Refresh basic profile fields (name, headline, public_id) for every
     tagged engager via Saleleads /user/profile (--refresh).
  4. Run demand_gen_headline_persona_classifier@1.0 (single GPT call per
     engager — headline+title+company only). Self-registers on first run.
  5. Save + run the per-leader bucket report. Export CSV.

CLI:
  python3 engager_persona_pipeline.py \\
    --client Acme-AI \\
    --leaders-tag carousel:marketers_new_role,carousel:traffic_awareness_trust,demand_gen_channel \\
    --report-name acme-ai-cross-carousel-2026-q2

  python3 engager_persona_pipeline.py \\
    --client Acme-AI \\
    --leaders-file ~/leaders.csv \\
    --report-name acme-ai-cross-carousel-2026-q2 \\
    --min-engagements 3 \\
    --max-posts-per-leader 50

Resumable: every phase auto-skips already-done work, so you can re-run
after a transient failure without losing progress.
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psycopg2
from lib import load_client_env, database_url, register_processor_from_yaml


# ── YAML spec for the headline-only persona classifier ────────────────────────
#
# Fuses gating (is_demand_marketer) + typing (practitioner/service_provider)
# into ONE gpt-4o-mini call per engager. No posts fetched, no activity
# signals computed — just reads headline + title + company + bio.
#
# Calibrated against the demand_gen_signals_enricher@1.1 v1.2 prompts (FCMO +
# DEMAND_MARKETER) — same exclusion rules (outbound-sales-as-a-service = no),
# same headline-parsing fallback when title/company are blank.

CLASSIFIER_NAME = "demand_gen_headline_persona_classifier"
CLASSIFIER_VERSION = "1.0"

CLASSIFIER_YAML = f"""
name: {CLASSIFIER_NAME}
version: "{CLASSIFIER_VERSION}"
processor_type: classifier

description: >
  Single-call headline-only classifier for B2B demand-gen persona. Reads
  headline + current_title + current_company + bio for a lead, returns
  is_demand_marketer (yes/no/unclear) AND type (practitioner / service
  provider / null) in one gpt-4o-mini call. Designed for the
  marketbase-engager-persona-report flow where we DON'T want to spend on
  per-engager post fetching + the 5-prompt demand_gen_signals_enricher.

  When to use this vs demand_gen_persona_classifier@1.0:
  - This (headline-only): cheap (~$0.001/lead), no Saleleads /user/posts
    call, no posts_3mo / avg_reactions_3mo signals → no activity_level.
    Good for "show me the persona-distribution of an engager pool".
  - demand_gen_persona_classifier@1.0: rules-based, requires the heavy
    demand_gen_signals_enricher to have run first. Better when you need
    to distinguish very_active vs active demand-gen pros.

inputs:
  fields_consulted:
    - leads.headline
    - leads.current_title
    - leads.current_company
    - leads.bio
    - leads.name
  prerequisites:
    - lead must have a non-empty headline (run fill_basic_profile first if blank)

outputs:
  writes_to_tables: [lead_qualifications]
  full_result_keys:
    - is_demand_marketer
    - type
    - persona
    - classification_reasoning

decision_rule: |
  Single GPT call returns the JSON shape:
    {{
      "is_demand_marketer": "yes" | "no" | "unclear",
      "type": "practitioner" | "service_provider" | null,
      "persona": "<human-readable derived string>",
      "classification_reasoning": "<3-sentence explanation>"
    }}

  qualified = (is_demand_marketer == "yes")
  persona is set in the prompt's logic to one of:
    "demand gen practitioner"
    "demand gen service provider"
    "demand gen — type unclear"   (is_dm=yes, type=null)
    "not demand gen"               (is_dm=no)
    "unclear"                      (is_dm=unclear)

logic:
  type: gpt-prompt
  system_prompt: |
    You are analyzing a LinkedIn profile to determine whether this person is part of the B2B demand-gen / marketing-growth professional community, and whether they're a practitioner (employed in-house at a company) or a service provider (agency/fractional/consultant serving multiple clients).

    You will receive their name, headline, current job title, current employer, and bio.

    Answer 2 questions plus reasoning, in one JSON.

    ───────────────────────────────────────────────
    Q1. is_demand_marketer — yes / no / unclear

    Answer YES if their primary professional identity matches any of these patterns:

    PRACTITIONERS & LEADERS
    - Demand gen, growth marketing, performance marketing for B2B
    - CMO / VP / Head of / Director of Marketing at a B2B company
    - ABM, paid media, SEO, content, lifecycle, email, events — any channel — applied to B2B
    - B2B marketing ops or analytics focused on pipeline/revenue
    - Brand or product marketing at a B2B company when clearly demand-adjacent
    - Field marketing, partner marketing, customer marketing at a B2B company

    ADVISORS & SERVICE PROVIDERS (for B2B MARKETING — not sales)
    - Fractional CMO, marketing advisor, growth consultant for B2B
    - Founder / operator of a B2B marketing-services agency or consultancy
    - Founder of a B2B marketing-tech / GTM-tech company

    THOUGHT LEADERS & EDUCATORS
    - Active B2B marketing thought leader who publishes about B2B demand, growth, GTM, ABM, content, paid media, or marketing leadership
    - B2B marketing podcaster, newsletter author, or community builder
    - Author of books / courses aimed at B2B marketing practitioners

    Hands-on channel execution is NOT required. Including thought leaders is intentional.

    CRITICAL — SALES SERVICES ≠ MARKETING SERVICES.
    Outbound sales, SDR-as-a-service, cold-call automation, appointment setting are SALES services, NOT marketing. Even if the headline says "B2B" and "pipeline" and "growth", if the work is selling sales-services, answer NO.

    Patterns that look marketing-ish but are SALES SERVICES → NO:
    - "B2B Outbound Specialist" / "Outbound Expert" / "Outbound Agency"
    - "SDR-as-a-Service" / "SDR agency" / "outsourced SDRs"
    - "Cold-call automation" / "Cold email at scale" / "Appointment setting"
    - "Pipeline-as-a-Service" when the methodology is cold outreach / outbound sales
    - "Build predictable pipeline" combined with cold-outreach signals → sales
    - Agencies selling "qualified meetings" via cold outreach → sales

    Answer NO if their primary focus is clearly:
    - Sales / SDR / BDR / sales enablement / sales recruiting (sales-only roles)
    - Outbound-as-a-service, SDR-as-a-service, appointment setting (see above)
    - Customer success, customer onboarding, post-sale retention
    - B2C marketing (consumer brands, retail, ecommerce DTC, CPG)
    - Specialized verticals OUTSIDE B2B (faith-based, real estate, consumer healthcare, celebrity/influencer brand work)
    - Non-marketing roles: engineering, finance, HR, ops, pure product management with no marketing scope
    - General business strategy or fundraising with no marketing identity

    Answer UNCLEAR only when title + company + headline + bio give NO signal about whether the person is in B2B marketing.

    ───────────────────────────────────────────────
    Q2. type — practitioner / service_provider / null

    Set type whenever the employment context is determinable, EVEN IF is_demand_marketer = "no" or "unclear" (the type field is independent of the gating).

    A. PRACTITIONER → "practitioner"
       The person is primarily an EMPLOYEE of one company, working on that company's own offering.
       Signs: clear job title at a named employer that's a product/tech/SaaS company (VP Marketing at Acme, Head of Growth at Beta); founder/CEO of a non-services product company; bio focuses on building the company's own product.

    B. SERVICE PROVIDER → "service_provider"
       The person primarily sells marketing/growth/GTM SERVICES to multiple clients.
       Signs: job title is Founder / CEO / Owner / Partner / Principal at a marketing/growth/GTM/agency/consultancy/services brand; company industry is Marketing Services, Consulting; bio uses language like "helping companies", "working with clients", "we act as an extension of your team"; headline includes "Fractional CMO", "Marketing Consultant", "Growth Advisor", "Agency Owner"; or they're explicitly Independent / Solo / Freelance.

    C. UNRECOGNIZED → null
       Only if employment context is genuinely unclear (blank title, vague headline like "Marketing Leader", no employer, no company description). Don't reach for null when the headline names a clear role + entity — parse "Founder of X" / "CMO @ Y" from the headline as evidence.

    ───────────────────────────────────────────────
    Q3. persona — derived string

    Set persona to ONE of:
    - "demand gen practitioner"          (is_dm=yes  AND type=practitioner)
    - "demand gen service provider"      (is_dm=yes  AND type=service_provider)
    - "demand gen — type unclear"        (is_dm=yes  AND type=null)
    - "not demand gen"                   (is_dm=no)
    - "unclear"                          (is_dm=unclear)

    ───────────────────────────────────────────────
    Q4. classification_reasoning — exactly 3 sentences

    1. What this person and their company do (cite the headline / parsed title if blank).
    2. Why is_demand_marketer = <your answer>.
    3. Why type = <your answer>.

    Respond ONLY with this JSON shape — no markdown, no extra text:
    {{
      "is_demand_marketer": "yes" | "no" | "unclear",
      "type": "practitioner" | "service_provider" | null,
      "persona": "<one of the 5 strings above>",
      "classification_reasoning": "<3 sentences>"
    }}

  prompt_template: |
    Name: {{{{ lead.name }}}}
    Current title: {{{{ lead.current_title }}}}
    Current employer: {{{{ lead.current_company }}}}
    Headline: {{{{ lead.headline }}}}
    Bio: {{{{ lead.bio }}}}

    (Any field above may be blank. Classify from whatever signal is present
    — at minimum the headline. Only return type=null when even the headline
    fails to name a clear role + entity.)

  output:
    qualified_key: is_demand_marketer
    qualified_value: yes
    persona_key: persona
    reason_key: classification_reasoning

rule_changes: |
  1.0 (initial): single-call headline-only classifier. Fuses the v1.2 FCMO
  prompt (employment_type detection) with the v1.2 is_demand_marketer
  prompt (B2B demand-gen gating, broader-than-v1.0 definition, explicit NO
  for outbound-sales-as-a-service). Output shape adds a derived `persona`
  string for back-compat with reports that expect it.
"""


# ── DB helpers ─────────────────────────────────────────────────────────────

def db_conn(db_url: str):
    # Retry transient DNS / network resolution failures to the Neon pooler
    # (intermittent "could not translate host name") with backoff, so a single
    # blip between phases doesn't kill the whole pipeline.
    import time as _t
    last = None
    for attempt in range(6):
        try:
            return psycopg2.connect(db_url, connect_timeout=20,
                                    keepalives=1, keepalives_idle=30,
                                    keepalives_interval=10, keepalives_count=3)
        except psycopg2.OperationalError as e:
            last = e
            if attempt < 5:
                _t.sleep(5 * (attempt + 1))
                continue
            raise
    raise last


# ── Pipeline phases ────────────────────────────────────────────────────────

def resolve_leaders(cur, leaders_tag: str | None, leaders_file: str | None) -> list[str]:
    """Return the list of leader LinkedIn URLs."""
    if leaders_file:
        path = os.path.expanduser(leaders_file)
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        urls = []
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            col = next((c for c in ("linkedin_url", "LinkedIn URL", "profile_url", "url")
                        if c in (reader.fieldnames or [])), None)
            if not col:
                raise ValueError(f"leaders file {path} has no URL column "
                                 f"(expected one of: linkedin_url, LinkedIn URL, profile_url, url)")
            for row in reader:
                u = (row.get(col) or "").strip()
                if u:
                    urls.append(u)
        return urls

    if leaders_tag:
        tags = [t.strip() for t in leaders_tag.split(",") if t.strip()]
        cur.execute("""
            SELECT DISTINCT l.linkedin_url
            FROM leads l JOIN lead_tags t ON t.lead_id=l.id
            WHERE t.tag = ANY(%s)
        """, (tags,))
        return [r[0] for r in cur.fetchall()]

    raise ValueError("must pass either --leaders-tag or --leaders-file")


def phase1_fetch_engagers(client: str, leaders: list[str], refresh: bool):
    """Per-leader subprocess fetch. On exit 2 (credit-exhausted), abort."""
    print(f"\n── Phase 1: fetch posts + engagers for {len(leaders)} leaders ──")
    fetcher = os.path.expanduser("~/.claude/tools/MarketBase/engager_pool_fetcher.py")
    ok = fail = 0
    for i, url in enumerate(leaders, 1):
        print(f"\n[{i}/{len(leaders)}] {url}  (ok={ok} fail={fail})")
        cmd = ["python3", "-u", fetcher, "--client", client, "--lead-url", url]
        if refresh:
            cmd.append("--refresh")
        rc = subprocess.call(cmd)
        if rc == 0:
            ok += 1
        elif rc == 2:
            print(f"\n  ✖ SaleleadsCreditExhausted — stopping phase 1. {ok} done, "
                  f"{len(leaders)-i} skipped. Resume by re-running with --refresh.")
            return False
        else:
            print(f"  ⚠ leader exit={rc} — continuing")
            fail += 1
    print(f"\n  phase 1 done: ok={ok} fail={fail}")
    return True


def phase2_tag_eligibles(cur, leaders: list[str], min_engagements: int,
                         engager_tag: str, tagged_by: str) -> int:
    """Identify >=N-engagement engagers + tag them."""
    print(f"\n── Phase 2: identify >={min_engagements}-engagement engagers ──")
    cur.execute("""
        CREATE TEMP TABLE IF NOT EXISTS _leaders (url text);
        TRUNCATE _leaders;
    """)
    cur.executemany("INSERT INTO _leaders(url) VALUES (%s)", [(u.lower().rstrip("/"),) for u in leaders])
    cur.execute("""
        WITH leader_ids AS (
          SELECT id, lower(linkedin_url) AS url FROM leads
          WHERE lower(linkedin_url) IN (SELECT lower(url) FROM _leaders)
             OR lower(linkedin_url)||'/' IN (SELECT lower(url)||'/' FROM _leaders)
        ),
        orig_posts AS (
          SELECT p.id FROM posts p JOIN leader_ids li ON lower(p.poster_linkedin_url)=li.url
          -- A reshare's author is someone OTHER than the leader; its reactors
          -- engaged with the original author's content, not this leader's. The
          -- author-slug match is the real signal — the legacy key checks below
          -- never fire on the current Saleleads payload shape.
          WHERE lower(substring(p.raw_data->'author'->>'url' from '/in/([^/?]+)'))
              = lower(substring(p.poster_linkedin_url from '/in/([^/?]+)'))
            AND COALESCE((p.raw_data->>'reshared')::boolean, false)=false
            AND COALESCE((p.raw_data->>'is_repost')::boolean, false)=false
            AND p.raw_data->>'repost_urn' IS NULL
            AND p.raw_data->>'reposted_post' IS NULL
            AND p.raw_data->>'shared_post' IS NULL
            AND COALESCE(p.raw_data->>'type', '') NOT IN ('REPOST', 'repost')
        ),
        eligible AS (
          SELECT pe.lead_id, COUNT(*) AS n
          FROM post_engagements pe JOIN orig_posts op ON pe.post_id=op.id
          WHERE pe.lead_id NOT IN (SELECT id FROM leader_ids)
          GROUP BY pe.lead_id HAVING COUNT(*) >= %s
        )
        INSERT INTO lead_tags (lead_id, tag, tagged_by, tagged_at)
        SELECT lead_id, %s, %s, now() FROM eligible
        ON CONFLICT (lead_id, tag) DO NOTHING
        RETURNING lead_id
    """, (min_engagements, engager_tag, tagged_by))
    inserted = len(cur.fetchall())
    cur.execute("SELECT COUNT(*) FROM lead_tags WHERE tag=%s", (engager_tag,))
    total = cur.fetchone()[0]
    print(f"  inserted {inserted} new tag rows; {total} total leads carry {engager_tag!r}")
    return total


def phase3_refresh_profiles(client: str, db_url: str, engager_tag: str,
                            batch_size: int = 100, max_batches: int = 1000):
    """fill_basic_profile per chunked batch.

    Why batched: fill_basic_profile holds a single Neon connection across all
    leads; for pools of thousands, the idle-in-transaction timeout cascades
    and re-runs with --refresh would re-fetch already-done leads (wasteful).

    Pattern: snapshot `cutoff_ts` at phase start; then loop until the DB has
    zero leads in the engager tag with `updated_at < cutoff_ts`. Each batch
    fetches the next chunk via --lead-file (no --refresh — we don't need it
    since cutoff_ts gates inclusion). DB connection drops in fill_basic_profile
    only lose the current batch; the next batch's DB query rebuilds the todo.
    """
    print(f"\n── Phase 3: refresh basic profile (batched) ──")
    fill = os.path.expanduser("~/.claude/tools/MarketBase/fill_basic_profile.py")
    todo_csv = f"/tmp/engager_persona_pipeline_phase3_todo_{os.getpid()}.csv"

    # Snapshot the cutoff at phase start. Anything updated AFTER this is "done
    # by phase 3"; anything updated BEFORE or null still needs refresh.
    with db_conn(db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT now()")
        cutoff_ts = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM lead_tags WHERE tag=%s", (engager_tag,))
        total_pool = cur.fetchone()[0]
    print(f"  cutoff_ts = {cutoff_ts}   pool size = {total_pool}")

    for batch in range(1, max_batches + 1):
        with db_conn(db_url) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT l.linkedin_url
                FROM lead_tags t JOIN leads l ON l.id=t.lead_id
                WHERE t.tag = %s
                  AND (l.updated_at IS NULL OR l.updated_at < %s)
                ORDER BY l.linkedin_url
                LIMIT %s
            """, (engager_tag, cutoff_ts, batch_size))
            urls = [r[0] for r in cur.fetchall()]

        if not urls:
            print(f"  phase 3 done after batch {batch-1}")
            return

        with open(todo_csv, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["linkedin_url"])
            for u in urls: w.writerow([u])

        print(f"  batch {batch}: {len(urls)} leads pending")
        rc = subprocess.call([
            "python3", "-u", fill, "--client", client,
            "--lead-file", todo_csv,
            "--refresh",  # force re-fetch even if headline already set
        ])
        if rc == 2:
            print(f"  ✖ phase 3 hit SaleleadsCreditExhausted. Stop.")
            sys.exit(2)
        if rc != 0:
            # DB connection cascade or similar — short sleep then continue.
            # Next batch's DB query rebuilds the todo from current state.
            print(f"    batch exit={rc}, continuing with fresh todo list…")
            time.sleep(5)

    print(f"  phase 3: hit max_batches={max_batches} cap — partial completion")


def phase4_classify(client: str, db_url: str, engager_tag: str):
    """Loop classify.py until all engagers have a persona qualification."""
    print(f"\n── Phase 4: persona classifier ({CLASSIFIER_NAME}@{CLASSIFIER_VERSION}) ──")
    classify = os.path.expanduser("~/.claude/tools/MarketBase/classify.py")
    for attempt in range(1, 12):
        with db_conn(db_url) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM lead_tags t
                WHERE t.tag=%s
                  AND NOT EXISTS (
                    SELECT 1 FROM lead_current_qualification q
                    WHERE q.lead_id=t.lead_id AND q.qualifier_name=%s
                  )
            """, (engager_tag, CLASSIFIER_NAME))
            remaining = cur.fetchone()[0]
        print(f"  attempt {attempt}: {remaining} still un-classified")
        if remaining == 0:
            break
        subprocess.call([
            "python3", "-u", classify,
            "--client", client,
            "--processor", CLASSIFIER_NAME,
            "--where-tag", engager_tag,
        ])
    print(f"  phase 4 done")


def phase5_save_and_run(client: str, db_url: str, engager_tag: str,
                        leaders_selector_sql: str, report_name: str,
                        out_csv: str | None):
    """Save the per-leader bucket report SQL + run it."""
    print(f"\n── Phase 5: save + run report '{report_name}' ──")

    report_sql = f"""
-- {report_name} — auto-generated by marketbase-engager-persona-report skill.
WITH leaders AS ({leaders_selector_sql}),
orig_posts AS (
  SELECT p.id, p.poster_linkedin_url FROM posts p
  WHERE lower(p.poster_linkedin_url) IN (SELECT lower(linkedin_url) FROM leaders)
    -- Exclude reshares: a reshared post's author slug differs from the leader
    -- we attributed it to (its reactors engaged with the original author, not
    -- the leader). This author-slug match is the only reshare signal present
    -- in the Saleleads payload; the legacy key checks never fire.
    AND lower(substring(p.raw_data->'author'->>'url' from '/in/([^/?]+)'))
      = lower(substring(p.poster_linkedin_url from '/in/([^/?]+)'))
    AND COALESCE((p.raw_data->>'reshared')::boolean, false)=false
    AND COALESCE((p.raw_data->>'is_repost')::boolean, false)=false
    AND p.raw_data->>'repost_urn' IS NULL
    AND p.raw_data->>'reposted_post' IS NULL
    AND p.raw_data->>'shared_post' IS NULL
    AND COALESCE(p.raw_data->>'type', '') NOT IN ('REPOST', 'repost')
),
eligible AS (
  SELECT ld.id AS leader_id, ld.name AS leader_name, ld.linkedin_url AS leader_url,
         pe.lead_id AS engager_id, COUNT(*) AS n
  FROM leaders ld
  JOIN orig_posts op ON lower(op.poster_linkedin_url)=lower(ld.linkedin_url)
  JOIN post_engagements pe ON pe.post_id=op.id
  WHERE pe.lead_id NOT IN (SELECT id FROM leaders)
  GROUP BY ld.id, ld.name, ld.linkedin_url, pe.lead_id
  HAVING COUNT(*) >= 3
),
classified AS (
  SELECT e.*,
         q.full_result->>'type' AS type,
         q.full_result->>'is_demand_marketer' AS is_dm,
         q.full_result->>'persona' AS persona
  FROM eligible e
  LEFT JOIN lead_current_qualification q
    ON q.lead_id=e.engager_id AND q.qualifier_name='{CLASSIFIER_NAME}'
)
SELECT
  leader_name AS thought_leader,
  leader_url AS linkedin_url,
  COUNT(*)                                                                    AS total_eligible_engagers,
  COUNT(*) FILTER (WHERE type='practitioner')                                 AS practitioner,
  COUNT(*) FILTER (WHERE type='service_provider')                             AS service_provider,
  COUNT(*) FILTER (WHERE is_dm='yes' AND type IS NULL)                        AS demand_gen_type_unclear,
  COUNT(*) FILTER (WHERE is_dm='no')                                          AS not_demand_gen,
  COUNT(*) FILTER (WHERE is_dm='unclear')                                     AS unclear,
  COUNT(*) FILTER (WHERE type IS NULL AND is_dm IS NULL)                      AS unclassified
FROM classified
GROUP BY leader_id, leader_name, leader_url
ORDER BY (
  COUNT(*) FILTER (WHERE type='practitioner') + COUNT(*) FILTER (WHERE type='service_provider')
) DESC, leader_name;
""".strip()

    # Save via marketbase-save-report
    save = os.path.expanduser("~/.claude/tools/MarketBase/save_report.py")
    rc = subprocess.call([
        "python3", save,
        "--client", client,
        "--name", report_name,
        "--sql", report_sql,
        "--description",
        f"Per thought-leader, count of >=3-engagement engagers by persona "
        f"({CLASSIFIER_NAME}@{CLASSIFIER_VERSION}). "
        f"Auto-generated by marketbase-engager-persona-report skill.",
        "--purpose",
        f"Created {date.today()}: who in each thought-leader's loyal audience "
        f"(>=3 engagements with original posts) is a B2B demand-gen practitioner "
        f"vs service provider vs non-target?",
        "--created-by", "marketbase-engager-persona-report",
    ])
    if rc != 0:
        print(f"  save_report failed exit={rc}")
        return

    # Run
    run = os.path.expanduser("~/.claude/tools/MarketBase/run_report.py")
    cmd = ["python3", run, "--client", client, "--name", report_name,
           "--ran-by", "marketbase-engager-persona-report"]
    if out_csv:
        cmd += ["--output", out_csv]
    subprocess.call(cmd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client", required=True)
    tg = ap.add_mutually_exclusive_group(required=True)
    tg.add_argument("--leaders-tag", help="Comma-joined OR list of tags identifying the leader set.")
    tg.add_argument("--leaders-file", help="CSV with a URL column.")
    ap.add_argument("--report-name", required=True,
                    help="Slug used for the saved report AND the engager:<report-name> tag.")
    ap.add_argument("--min-engagements", type=int, default=3)
    ap.add_argument("--max-posts-per-leader", type=int, default=50)
    ap.add_argument("--no-refetch", action="store_true",
                    help="Skip phase 1 (assume leaders already fetched recently).")
    ap.add_argument("--no-refresh-profile", action="store_true",
                    help="Skip phase 3 (use whatever headlines are already in the DB).")
    ap.add_argument("--out", help="CSV output path for the final report. "
                                  "Defaults to stdout-only.")
    args = ap.parse_args()

    load_client_env(args.client)
    db_url = database_url(args.client)
    engager_tag = f"engager:{args.report_name}"

    # Register classifier (idempotent — same yaml = no-op)
    try:
        _, _, action = register_processor_from_yaml(
            args.client, CLASSIFIER_YAML, created_by="marketbase-engager-persona-report")
        if action == "inserted":
            print(f"✓ registered processor {CLASSIFIER_NAME}@{CLASSIFIER_VERSION}")
    except Exception as e:
        print(f"⚠ self-registration failed: {e}", file=sys.stderr)
        return 1

    # Resolve leaders
    with db_conn(db_url) as conn, conn.cursor() as cur:
        leaders = resolve_leaders(cur, args.leaders_tag, args.leaders_file)
    if not leaders:
        print("✖ no leaders resolved", file=sys.stderr)
        return 2
    print(f"=== {args.client} | report={args.report_name} | "
          f"engager_tag={engager_tag} | leaders={len(leaders)} ===")

    # Build leader selector SQL for the report (mirrors how we resolved leaders)
    if args.leaders_tag:
        tags = [t.strip() for t in args.leaders_tag.split(",")]
        tag_list = ", ".join(f"'{t}'" for t in tags)
        leaders_selector_sql = (f"SELECT DISTINCT l.id, l.name, l.linkedin_url FROM leads l "
                                f"JOIN lead_tags t ON t.lead_id=l.id WHERE t.tag IN ({tag_list})")
    else:
        # File-mode: hard-code the URL list into the report SQL so it's self-contained.
        # Normalise trailing slashes on both sides — leads.linkedin_url is stored
        # without a trailing slash, but file URLs often carry one, and an exact
        # IN match would silently drop those leaders from the report.
        url_list = ", ".join(f"lower({repr(u.lower().rstrip('/'))})" for u in leaders)
        leaders_selector_sql = (f"SELECT id, name, linkedin_url FROM leads "
                                f"WHERE lower(rtrim(linkedin_url,'/')) IN ({url_list})")

    # 1
    if not args.no_refetch:
        if not phase1_fetch_engagers(args.client, leaders, refresh=True):
            return 2
    else:
        print("\n── Phase 1: SKIPPED (--no-refetch) ──")

    # 2
    with db_conn(db_url) as conn, conn.cursor() as cur:
        n_engagers = phase2_tag_eligibles(cur, leaders, args.min_engagements,
                                          engager_tag, "marketbase-engager-persona-report")
        conn.commit()
    if n_engagers == 0:
        print("✖ 0 engagers eligible — nothing to classify"); return 2

    # 3
    if not args.no_refresh_profile:
        phase3_refresh_profiles(args.client, db_url, engager_tag)
    else:
        print("\n── Phase 3: SKIPPED (--no-refresh-profile) ──")

    # 4
    phase4_classify(args.client, db_url, engager_tag)

    # 5
    phase5_save_and_run(args.client, db_url, engager_tag,
                        leaders_selector_sql, args.report_name, args.out)

    print(f"\n=== pipeline done ===\n"
          f"  saved report:   {args.report_name}  (re-run with `marketbase-run-report`)\n"
          f"  engager tag:    {engager_tag}\n"
          f"  classifier:     {CLASSIFIER_NAME}@{CLASSIFIER_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
