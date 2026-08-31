"""Shared library for all marketbase-* skills.

The convention:
  - Each client has a Neon project (Impact 11 org).
  - The Postgres connection string lives in ~/.env.<ClientName>
    as <ClientName>_DATABASE_URL (e.g. ACME_DATABASE_URL).
  - All MarketBase skills accept --client <ClientName> and read the corresponding
    env file via load_client_env().
"""
from __future__ import annoacmens

import os
import re
import sys
from pathlib import Path


_URL_PARTS_RE = re.compile(r"^(https?://)?([^/]+)(/.*)?$", re.I)

def normalize_linkedin_url(u: str | None) -> str:
    """Canonical form used as the unique key for leads.linkedin_url.

    Lowercases scheme + host. PRESERVES path case — LinkedIn's URN-encoded
    slugs (`/in/ACoAA...`, `/in/ACwAA...`) are case-sensitive when used
    against the saleleads / Fresh LinkedIn APIs. Strips trailing slash,
    query string, and fragment. Forces www.linkedin.com host."""
    if not u: return ""
    u = u.strip()
    if not u: return ""
    # Drop query string / fragment FIRST so they don't pollute the path
    u = u.split("?")[0].split("#")[0]
    # Pull apart scheme, host, path
    m = _URL_PARTS_RE.match(u)
    if not m:
        # Bare path or junk — return stripped/case-preserved with trailing slash off
        return u.rstrip("/")
    scheme = (m.group(1) or "https://").lower()
    if scheme == "http://": scheme = "https://"
    host = (m.group(2) or "").lower()
    if host == "linkedin.com": host = "www.linkedin.com"
    path = (m.group(3) or "").rstrip("/")
    return f"{scheme}{host}{path}"


_URN_RE = re.compile(r"/in/([A-Za-z0-9_\-]+)")
def linkedin_urn(u: str | None) -> str:
    if not u: return ""
    m = _URN_RE.search(u)
    return (m.group(1) if m else "")


# The stable LinkedIn member URN token (the `AC…` blob). Members span several
# prefixes (ACoAA, ACwAA, ACEAA, …) — match the whole family, not just ACoAA.
# This is the canonical person identity (leads.member_urn).
_MEMBER_URN_RE = re.compile(r"AC[A-Za-z0-9_\-]{15,}")
def member_urn_token(*vals: str | None) -> str | None:
    """Return the first `AC…` member-URN token found across the given values
    (e.g. a linkedin_url and a urn hint). None if none carry one."""
    for v in vals:
        if v:
            m = _MEMBER_URN_RE.search(v)
            if m:
                return m.group(0)
    return None


def resolve_canonical_url(cur, linkedin_url: str | None, *,
                          urn_hint: str | None = None) -> str | None:
    """Map an incoming (normalized) linkedin_url to the linkedin_url of the
    EXISTING lead that represents this person, so an upsert keyed on
    `ON CONFLICT (linkedin_url)` updates the canonical row instead of creating a
    duplicate (or violating the uq_leads_member_urn identity index).

    Match order: stable member URN (from the url or the urn hint) → vanity slug
    against a stored public_id. Returns the input url unchanged when nothing
    matches (genuinely new person). Pure cur reads — no writes."""
    if not linkedin_url:
        return linkedin_url
    token = member_urn_token(linkedin_url, urn_hint)
    if token:
        cur.execute("SELECT linkedin_url FROM leads WHERE member_urn = %s LIMIT 1",
                    (token,))
        row = cur.fetchone()
        if row:
            return row[0]
    m = _URN_RE.search(linkedin_url)
    slug = m.group(1) if m else None
    if slug and not slug.startswith("AC"):
        cur.execute("SELECT linkedin_url FROM leads WHERE public_id = %s LIMIT 1",
                    (slug,))
        row = cur.fetchone()
        if row:
            return row[0]
    return linkedin_url


def env_path(client: str) -> Path:
    """~/.env.<ClientName> — per-client secrets file."""
    return Path.home() / f".env.{client}"


def load_client_env(client: str) -> dict[str, str]:
    """Reads ~/.env.<ClientName> + the global ~/.env (the latter as fallback)
    so client-specific keys can override / extend the global ones.

    Returns the merged dict, also sets matching env vars in os.environ."""
    env = {}
    for p in (Path.home() / ".env", env_path(client)):
        if not p.exists(): continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in env.items():
        os.environ.setdefault(k, v)
    return env


def database_url(client: str) -> str:
    """The connection string for this client's Neon DB.

    Tries (in order):
      1. <CLIENT>_DATABASE_URL     ← preferred convention
      2. DATABASE_URL              ← generic
      3. LEAD_DB_CONNSTRING        ← legacy convention used in some envs
    """
    env = load_client_env(client)
    # Per the per-workspace-env-files convention, prefer the plain unsuffixed
    # var names inside ~/.env.<Client>. Accept legacy variants too.
    for key in ("GTM_DB_CONNSTRING", "DATABASE_URL",
                f"{client.upper()}_DATABASE_URL", "LEAD_DB_CONNSTRING"):
        url = env.get(key)
        if url:
            return url
    sys.exit(
        f"No database URL found in ~/.env.{client}. "
        f"Expected one of: GTM_DB_CONNSTRING, DATABASE_URL, "
        f"{client.upper()}_DATABASE_URL, LEAD_DB_CONNSTRING. "
        f"Run marketbase-init-client first."
    )


def connect(client: str):
    """Return a psycopg connection to the client's DB."""
    import psycopg
    return psycopg.connect(database_url(client), autocommit=False)


# Schema migrations — applied in order on init.
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
MIGRATIONS = sorted(SCHEMA_DIR.glob("*.sql"))


_TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def applied_migrations(client: str) -> set[str]:
    """Returns the set of migration filenames already applied to this client's DB."""
    with connect(client) as conn:
        with conn.cursor() as cur:
            cur.execute(_TRACKING_TABLE_SQL)
            conn.commit()
            cur.execute("SELECT filename FROM schema_migrations")
            return {r[0] for r in cur.fetchall()}


_RE_COMPANY_SLUG = re.compile(r"linkedin\.com/company/([A-Za-z0-9_\-]+)", re.I)

def _derive_company_slug(linkedin_url: str | None, name: str | None) -> str:
    """Best-effort slug derivation. Prefers parsing from a LinkedIn URL,
    falls back to slugifying the company name. Returns empty string if
    neither is usable — caller should skip the upsert in that case."""
    if linkedin_url:
        m = _RE_COMPANY_SLUG.search(linkedin_url)
        if m:
            return m.group(1).lower()
    if name:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if slug:
            return slug
    return ""


def upsert_company_from_qualification(cur, full_result: dict) -> str:
    """UPSERT a row in the client's `companies` table from a
    qualify-acme-target full_result dict (or equivalent Qualification
    asdict). Matches on `linkedin_slug` if derivable; falls back to lower(name).
    COALESCE-fills empty columns (does not overwrite curated data). Returns
    the company UUID as text, or '' if there wasn't enough data to upsert.

    Generic — works for any client MarketBase because the `companies` table shape
    is part of the MarketBase migrations (see schema/001_identity.sql)."""
    if not isinstance(full_result, dict):
        return ""
    name = (full_result.get("current_company") or "").strip()
    if not name:
        return ""
    linkedin_url = (full_result.get("company_linkedin_url") or "").strip() or None
    linkedin_slug = (full_result.get("company_linkedin_slug") or "").strip().lower()
    if not linkedin_slug:
        linkedin_slug = _derive_company_slug(linkedin_url, name)
    if not linkedin_slug:
        return ""  # nothing to key on
    industry = (full_result.get("company_industry") or "").strip() or None
    website = (full_result.get("company_website") or "").strip() or None
    employee_count = full_result.get("employee_count")
    employee_range = (full_result.get("employee_range") or "").strip() or None

    cur.execute("""
        INSERT INTO companies (linkedin_slug, linkedin_url, name,
                               website, industry, employee_count, employee_range,
                               size_fetched_at, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW())
        ON CONFLICT (linkedin_slug) DO UPDATE SET
            linkedin_url   = COALESCE(NULLIF(companies.linkedin_url, ''),   EXCLUDED.linkedin_url),
            name           = COALESCE(NULLIF(companies.name, ''),           EXCLUDED.name),
            website        = COALESCE(NULLIF(companies.website, ''),        EXCLUDED.website),
            industry       = COALESCE(NULLIF(companies.industry, ''),       EXCLUDED.industry),
            employee_count = COALESCE(companies.employee_count,             EXCLUDED.employee_count),
            employee_range = COALESCE(NULLIF(companies.employee_range, ''), EXCLUDED.employee_range),
            size_fetched_at= COALESCE(companies.size_fetched_at,            NOW()),
            updated_at     = NOW()
        RETURNING id
    """, (linkedin_slug, linkedin_url, name, website, industry, employee_count, employee_range))
    return str(cur.fetchone()[0])


def register_processor_from_yaml(client: str, yaml_text: str, *, created_by: str | None = None) -> tuple[str, str, str]:
    """UPSERT a processor row in the client's MarketBase from a YAML spec.

    Returns (name, version, action) where action is 'inserted' or 'updated'.

    Skips silently (returns ('', '', 'skipped')) if the processors table
    doesn't exist yet — useful so client orchestrators can self-register
    without breaking when run against a DB that pre-dates migration 015.

    The YAML must declare top-level `name`, `version`, and `processor_type`.
    Other fields (`description`, `inputs`, `outputs`, `rule_changes`) are
    optional and extracted for queryability."""
    import yaml as _yaml
    from psycopg.types.json import Jsonb as _Jsonb

    spec = _yaml.safe_load(yaml_text)
    if not isinstance(spec, dict):
        raise ValueError("YAML must be a mapping at the top level.")
    name = str(spec["name"])
    version = str(spec["version"])
    # Accept either processor_type (new) or process_type (legacy) for backwards-compat
    processor_type = str(spec.get("processor_type") or spec.get("process_type"))
    description = spec.get("description")
    inputs = spec.get("inputs")
    outputs = spec.get("outputs")
    rule_changes = spec.get("rule_changes")

    with connect(client) as conn:
        with conn.cursor() as cur:
            # Silent no-op if the processors table doesn't exist yet.
            cur.execute("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema='public' AND table_name='processors'
            """)
            if not cur.fetchone():
                return ('', '', 'skipped')

            cur.execute("SELECT id FROM processors WHERE name = %s AND version = %s",
                        (name, version))
            existing = cur.fetchone()
            if existing:
                cur.execute("""
                    UPDATE processors SET
                      processor_type = %s, description = %s, yaml_spec = %s,
                      inputs = %s, outputs = %s, rule_changes = %s,
                      created_by = COALESCE(%s, created_by)
                    WHERE id = %s
                """, (processor_type, description, yaml_text,
                      _Jsonb(inputs) if inputs is not None else None,
                      _Jsonb(outputs) if outputs is not None else None,
                      rule_changes, created_by, existing[0]))
                action = 'updated'
            else:
                cur.execute("""
                    INSERT INTO processors
                      (name, version, processor_type, description, yaml_spec,
                       inputs, outputs, rule_changes, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (name, version, processor_type, description, yaml_text,
                      _Jsonb(inputs) if inputs is not None else None,
                      _Jsonb(outputs) if outputs is not None else None,
                      rule_changes, created_by))
                action = 'inserted'
        conn.commit()
    return (name, version, action)


# Backwards-compat alias for callers that haven't migrated yet
register_process_from_yaml = register_processor_from_yaml


def apply_schema(client: str, *, backfill_existing: bool = True) -> dict[str, list[str]]:
    """Apply every migration in order, skipping ones already recorded in the
    schema_migrations table. Returns {'applied': [...], 'skipped': [...]}.

    On the very first run against a DB that pre-dates the tracking table
    (i.e. created before this change), `backfill_existing=True` will mark all
    pre-existing migrations as applied IF the DB already has the `leads`
    table (the canonical indicator that 001_identity has been run). This
    avoids re-running idempotent-but-noisy SQL on legacy clients.
    """
    with connect(client) as conn:
        with conn.cursor() as cur:
            cur.execute(_TRACKING_TABLE_SQL)
            conn.commit()
            cur.execute("SELECT filename FROM schema_migrations")
            already = {r[0] for r in cur.fetchall()}

            # Heuristic backfill: if the tracking table is empty but `leads`
            # exists, assume migrations 001-007 have already been applied
            # against this DB (the pre-tracking era) and record them.
            if backfill_existing and not already:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema='public' AND table_name='leads'
                """)
                if cur.fetchone():
                    for fn in (f.name for f in MIGRATIONS if f.name < "008_"):
                        cur.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s) "
                            "ON CONFLICT (filename) DO NOTHING", (fn,))
                    conn.commit()
                    cur.execute("SELECT filename FROM schema_migrations")
                    already = {r[0] for r in cur.fetchall()}

        applied, skipped = [], []
        for sql_file in MIGRATIONS:
            if sql_file.name in already:
                skipped.append(sql_file.name)
                continue
            with conn.cursor() as cur:
                cur.execute(sql_file.read_text())
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s) "
                    "ON CONFLICT (filename) DO NOTHING", (sql_file.name,))
            conn.commit()
            applied.append(sql_file.name)
    return {"applied": applied, "skipped": skipped}
