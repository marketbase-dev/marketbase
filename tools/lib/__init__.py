"""Shared library for all marketbase-* skills.

The convention:
  - Each MarketBase instance has a Neon project (Impact 11 org).
  - The Postgres connection string lives in that instance's own Infisical
    project (see ../../infisical_credentials.json), falling back to
    ~/.env.<ClientName> for instances not yet migrated off env files.
  - All marketbase skills accept --client <ClientName> and read the corresponding
    env file via load_client_env().
"""
from __future__ import annotations

import json
import os
import re
import subprocess
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

    # Infisical fills anything the env files did not provide. Deliberately
    # LAST, so existing env-file precedence is unchanged and this is additive:
    # secrets already migrated out of ~/.env resolve here instead.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from secret_store import hydrate
        for k in hydrate(client):
            env.setdefault(k, os.environ[k])
    except Exception:
        pass

    return env


# One instance->project map for the whole machine. `configs/infisical_projects.json`
# used to be a second, MarketBase-only registry whose presence in it *was* the
# conversion step; that is scrapped. A client is on MarketBase because its own
# Infisical project holds MARKETBASE_DB_CONNSTRING — the secret's existence is
# the registration, with no second list to keep in sync.
INFISICAL_MAP = Path(__file__).resolve().parent.parent.parent / "infisical_credentials.json"

# Exactly one accepted name. The old chain also took DATABASE_URL and
# GTM_DB_CONNSTRING, so a client missing its MarketBase secret connected to its
# own LIVE gtmdb and migrated it. Named to mirror GTM_DB_CONNSTRING so the two
# tracks read as siblings.
DB_KEY = "MARKETBASE_DB_CONNSTRING"


def infisical_instance(client: str) -> dict | None:
    """The Infisical project mapping for this instance, or None if unmapped."""
    try:
        cfg = json.loads(INFISICAL_MAP.read_text())
    except Exception:
        return None
    return (cfg.get("scopes") or {}).get(client)


def infisical_secret(client: str, keys: tuple[str, ...]) -> str | None:
    """Read the first present secret in `keys` from the instance's Infisical
    project. Returns None when the instance is unmapped, the CLI is missing,
    or no key is set — callers fall back to ~/.env.<Client>."""
    inst = infisical_instance(client)
    if not inst:
        return None
    for key in keys:
        try:
            r = subprocess.run(
                ["infisical", "secrets", "get", key,
                 "--projectId", inst["projectId"],
                 "--env", inst.get("env", "dev"),
                 "--plain", "--silent"],
                capture_output=True, text=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def discover_instances() -> list[str]:
    """Every client whose Infisical project holds MARKETBASE_DB_CONNSTRING.

    Holding that secret IS being a MarketBase instance. This deliberately does
    NOT glob ~/.env.* the way the gtmdb toolkit does: gtmdb and MarketBase are
    parallel tracks sharing one machine, and globbing would push MarketBase
    migrations straight into live gtmdb client databases.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from secret_store import scoped_secret
    try:
        scopes = json.loads(INFISICAL_MAP.read_text()).get("scopes") or {}
    except Exception:
        return []
    return sorted(sc for sc in scopes
                  if not sc.startswith("__") and scoped_secret(DB_KEY, sc))


def database_url(client: str) -> str:
    """The connection string for this instance's MarketBase DB, from
    `MARKETBASE_DB_CONNSTRING` resolved in this client's scope ONLY.

    Two fallbacks used to live here and both led to the same place. Falling back
    to GTM_DB_CONNSTRING meant a client without a MarketBase secret connected to
    its own live gtmdb — and marketbase-init-client would have migrated it.
    Falling through Infisical scopes (__private__ then __team__) meant resolving
    to *Impact 11's* MarketBase database, which really does hold this key.
    Neither failure said anything; both wrote to the wrong database.
    """
    load_client_env(client)   # keeps every other secret resolving as before
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from secret_store import scoped_secret
    url = scoped_secret(DB_KEY, client)
    if url:
        return url
    sys.exit(
        f"No {DB_KEY} for {client!r} in its own Infisical project or "
        f"~/.env.{client}, so {client!r} is not a MarketBase instance.\n"
        f"  If it should be, run marketbase-init-client.\n"
        f"  If it is still on gtmdb, use ~/.claude/tools/gtmdb/ instead.\n"
        f"(Only {DB_KEY} is accepted, and only from this client's own scope — "
        f"the old fallbacks resolved to a live gtmdb or to another instance's "
        f"database.)"
    )


def connect(client: str, *, _skip_identity: bool = False):
    """Return a psycopg connection to the instance's MarketBase DB.

    The database is asked to prove it is the one requested. This matters most
    right after a clone, when the copy is byte-identical to its gtmdb source and
    nothing else can tell them apart. Only the stamping tool skips the check.
    """
    import psycopg
    conn = psycopg.connect(database_url(client), autocommit=False)
    if _skip_identity:
        return conn
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from db_identity import assert_identity
    try:
        assert_identity(conn, client, "marketbase")
    except Exception:
        conn.close()
        raise
    # Stamp WHO is touching this database, for the audit trail that
    # schema/050_lead_tag_removal_audit.sql writes on every tag removal.
    # Derived from the entry-point script so attribution costs callers nothing:
    # the tool that forgets to identify itself is exactly the one whose history
    # you will want later. Session scope, not SET LOCAL, which would die with
    # the first transaction.
    try:
        raw = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
        actor = (raw or "").removesuffix(".py")
        if actor in ("", "-", "python", "python3", "-c"):
            # stdin, heredoc or REPL: the ad-hoc case this audit exists for, so
            # say so plainly rather than recording a meaningless "-"
            import getpass
            actor = f"adhoc:{getpass.getuser()}"
        with conn.cursor() as _cur:
            _cur.execute("SELECT set_config('marketbase.actor', %s, false)",
                         (actor[:60],))
        conn.commit()
    except Exception:
        pass          # attribution is never worth failing a connection over
    return conn


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

    Generic — works for any client marketbase because the `companies` table shape
    is part of the marketbase migrations (see schema/001_identity.sql)."""
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
    """UPSERT a processor row in the client's marketbase from a YAML spec.

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
