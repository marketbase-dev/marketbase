#!/usr/bin/env python3
"""Regenerate marketbase_CONTEXT_<Client>.md inside the customer's GTM folder.

Bundle contents (intentionally lean — anything queryable on demand is OUT):
  1. Header (client + DB host + generated_at + inputs_hash footer at bottom)
  2. Conventions (verbatim copy of CONVENTIONS.md)
  3. Schema model (the three-pillar narrative)
  4. Schema reference (live query against this client's DB)
  5. Skill catalog (frontmatter from each ~/.claude/skills/marketbase-*/SKILL.md)

Usage:
  python3 rebuild_context.py --client Acme
  python3 rebuild_context.py --all
  python3 rebuild_context.py --client Acme --force   # ignore hash match
"""
from __future__ import annoacmens

import argparse
import hashlib
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, database_url, load_client_env  # noqa: E402


MARKETBASE_ROOT = Path(__file__).resolve().parent
SKILLS_ROOT = Path.home() / ".claude" / "skills"
CONVENTIONS = MARKETBASE_ROOT / "CONVENTIONS.md"
SCHEMA_DIR = MARKETBASE_ROOT / "schema"
CUSTOMERS_ROOT = Path(
    "<your-documents-root>/"
    "My Drive/Impact11/Customers and Partners"
)

HASH_FOOTER_RE = re.compile(r"<!--\s*inputs_hash:\s*([0-9a-f]+)\s*-->")


def inputs_hash() -> str:
    """SHA1 over the three input sources that shape the bundle."""
    h = hashlib.sha1()
    h.update(b"v1\n")  # bump if the renderer changes shape
    files: list[Path] = [CONVENTIONS]
    files += sorted(SCHEMA_DIR.glob("*.sql"))
    files += sorted(SKILLS_ROOT.glob("marketbase-*/SKILL.md"))
    for f in files:
        h.update(f.name.encode())
        h.update(b"\0")
        try:
            h.update(f.read_bytes())
        except FileNotFoundError:
            h.update(b"<missing>")
        h.update(b"\n")
    return h.hexdigest()


def existing_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = path.read_text()
    except Exception:
        return None
    m = HASH_FOOTER_RE.search(data[-2048:])
    return m.group(1) if m else None


def client_gtm_dir(client: str) -> Path:
    return CUSTOMERS_ROOT / client / f"{client} GTM"


def bundle_path(client: str) -> Path:
    return client_gtm_dir(client) / f"marketbase_CONTEXT_{client}.md"


def discover_clients() -> list[str]:
    """Every ~/.env.<Name> file that has a GTM_DB_CONNSTRING value."""
    out = []
    for p in Path.home().glob(".env.*"):
        if p.name.endswith(".bak"):
            continue
        name = p.name[len(".env."):]
        if not name:
            continue
        try:
            text = p.read_text()
        except Exception:
            continue
        if "GTM_DB_CONNSTRING" in text:
            out.append(name)
    out.sort()
    return out


# ---- schema reference (live query) -----------------------------------------

def fetch_schema_reference(client: str) -> str:
    """Render tables + columns + views + enums from the live DB."""
    load_client_env(client)
    lines: list[str] = []

    with connect(client) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema='public' AND table_type='BASE TABLE'
                  AND table_name NOT LIKE 'pg_%'
                ORDER BY table_name
            """)
            tables = [r[0] for r in cur.fetchall()]

            cur.execute("""
                SELECT table_name, column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema='public'
                ORDER BY table_name, ordinal_position
            """)
            cols_by_table: dict[str, list] = {}
            for tn, cn, dt, nullable, default in cur.fetchall():
                cols_by_table.setdefault(tn, []).append((cn, dt, nullable, default))

            cur.execute("""
                SELECT table_name FROM information_schema.views
                WHERE table_schema='public'
                ORDER BY table_name
            """)
            views = [r[0] for r in cur.fetchall()]

            cur.execute("""
                SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder)
                FROM pg_type t
                JOIN pg_enum e ON t.oid = e.enumtypid
                JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname='public'
                GROUP BY t.typname
                ORDER BY t.typname
            """)
            enums = cur.fetchall()

    lines.append("### Tables")
    lines.append("")
    for t in tables:
        lines.append(f"#### `{t}`")
        lines.append("")
        lines.append("| Column | Type | Nullable | Default |")
        lines.append("|---|---|---|---|")
        for cn, dt, nullable, default in cols_by_table.get(t, []):
            d = (default or "").replace("|", "\\|")
            if len(d) > 60:
                d = d[:57] + "…"
            lines.append(f"| `{cn}` | `{dt}` | {nullable} | {d or ''} |")
        lines.append("")

    if views:
        lines.append("### Views")
        lines.append("")
        for v in views:
            lines.append(f"- `{v}`")
        lines.append("")

    if enums:
        lines.append("### Enums")
        lines.append("")
        for name, labels in enums:
            vals = ", ".join(f"`{x}`" for x in labels)
            lines.append(f"- `{name}`: {vals}")
        lines.append("")

    return "\n".join(lines)


# ---- skill catalog ---------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def render_skill_catalog() -> str:
    rows: list[tuple[str, str]] = []
    for skill_md in sorted(SKILLS_ROOT.glob("marketbase-*/SKILL.md")):
        try:
            text = skill_md.read_text()
        except Exception:
            continue
        fm = parse_frontmatter(text)
        name = fm.get("name") or skill_md.parent.name
        desc = fm.get("description") or ""
        rows.append((name, desc))
    if not rows:
        return "_(no marketbase-* skills found)_\n"
    out = ["| Skill | What it does |", "|---|---|"]
    for n, d in rows:
        d = d.replace("|", "\\|")
        out.append(f"| `{n}` | {d} |")
    return "\n".join(out) + "\n"


# ---- bundle render ---------------------------------------------------------

SCHEMA_MODEL_NARRATIVE = """\
The MarketBase is organized around three pillars of how we track a person:

1. **Provenance** — `lead_sources` is append-only and immutable. Every row
   records "we first saw / re-saw this person via source X at time T". A lead
   can have many source rows; none ever change.

2. **Decisions** — `lead_qualifications` is append-only and algorithmic.
   Every classifier run (current or historical) writes one row with the
   inputs, the verdict, and the full audit trail in `full_result` JSONB.
   `lead_current_qualification` is the convenience view that returns the
   most recent qualification per lead.

3. **State** — `lead_tags` is mutable, multi-valued, and the index into
   everything above. Tags use the `<category>:<value>` convention (see
   Conventions, Tag categories). They never carry reasoning — reasoning
   lives in the underlying `lead_qualifications.full_result` or
   `lead_actions` row.

Two adjacent surfaces:

- **`lead_actions`** is a one-shot intent queue ("please do this specific
  thing for this lead right now"). It complements tags, which are durable.
- **`processors`** is the versioned registry of every classifier / fetcher /
  reporter / enricher / orchestrator that touches the DB. Every
  `lead_qualifications` row references a processor by `qualifier_name` +
  `qualifier_version` — bumping the version when rules change keeps history
  re-derivable.

The campaign side mirrors this shape:

- **`campaigns`** + **`campaign_members`** with a status enum and a trigger
  that appends each transition to `status_history` JSONB. Sequencers
  (Smartlead / Smartlead / Dripify / etc.) are consumers — they read from MarketBase,
  act, and write back `status` + `last_status_source`.
"""


def render_bundle(client: str) -> str:
    load_client_env(client)
    db_url = database_url(client)
    host = ""
    m = re.search(r"@([^/]+)/", db_url)
    if m:
        host = m.group(1)

    parts: list[str] = []
    parts.append(f"# MarketBase context — {client}")
    parts.append("")
    parts.append(
        "This file is the canonical, machine-and-human-readable description "
        "of the MarketBase that holds this client's leads, qualifications, "
        "campaigns, and engagement history. It is regenerated automatically "
        "whenever the underlying conventions, schema, or skills change — "
        "treat it as read-only."
    )
    parts.append("")
    parts.append(f"- **Client**: {client}")
    parts.append(f"- **Neon DB host**: `{host}`")
    parts.append(
        f"- **Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}"
    )
    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append("## Conventions")
    parts.append("")
    parts.append(
        "_The following section is a verbatim copy of "
        "`~/.claude/tools/MarketBase/CONVENTIONS.md` — the behavior contract "
        "shared by every MarketBase skill and orchestrator._"
    )
    parts.append("")
    parts.append(CONVENTIONS.read_text().rstrip())
    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append("## Schema model")
    parts.append("")
    parts.append(SCHEMA_MODEL_NARRATIVE.rstrip())
    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append("## Schema reference (live)")
    parts.append("")
    parts.append(
        f"_Pulled from `{host}` at generation time. To re-derive ad-hoc, query "
        "`information_schema.tables` / `information_schema.columns` / "
        "`pg_type`+`pg_enum`._"
    )
    parts.append("")
    parts.append(fetch_schema_reference(client))
    parts.append("---")
    parts.append("")
    parts.append("## Skill catalog")
    parts.append("")
    parts.append(
        "_Every skill whose folder name starts with `marketbase-` under "
        "`~/.claude/skills/`. Description is the `description:` line from "
        "each skill's frontmatter._"
    )
    parts.append("")
    parts.append(render_skill_catalog())
    parts.append("---")
    parts.append("")
    parts.append("## On-demand queries")
    parts.append("")
    parts.append(
        "Things deliberately NOT embedded above, because they change often "
        "and are cheap to ask the DB for when you need them:"
    )
    parts.append("")
    parts.append(
        "- **Migrations applied here**: `SELECT filename, applied_at FROM schema_migrations ORDER BY applied_at`"
    )
    parts.append(
        "- **Registered processors**: `SELECT name, version, processor_type, description FROM processors ORDER BY name, version`"
    )
    parts.append(
        "- **Saved reports**: `SELECT name, description FROM saved_reports ORDER BY name`"
    )
    parts.append("")
    parts.append(f"<!-- inputs_hash: {inputs_hash()} -->")
    return "\n".join(parts) + "\n"


# ---- webhook notification -------------------------------------------------

def notify_swan_context_update(client: str) -> None:
    """POST an empty JSON body to SEQUENCER_CONTEXT_UPDATE_WEBHOOK_URL (if set in
    `~/.env.<Client>`) after a successful marketbase_CONTEXT rebuild.

    Reads the URL from `load_client_env(client)`'s RETURN dict (not
    os.environ) so we get the per-client value — `os.environ.setdefault`
    would otherwise stick the first client's URL across the whole run.

    Never raises — webhook failures must not break the rebuild."""
    try:
        env = load_client_env(client)
        url = (env.get("SEQUENCER_CONTEXT_UPDATE_WEBHOOK_URL") or "").strip()
        if not url:
            return
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as _:
            pass
    except Exception:
        pass


# ---- top-level orchestration -----------------------------------------------

def rebuild_one(client: str, *, force: bool = False, verbose: bool = False) -> str:
    """Returns one of: 'skipped', 'rebuilt', 'no_folder', 'no_db', 'error:<msg>'."""
    gtm_dir = client_gtm_dir(client)
    if not gtm_dir.exists():
        return "no_folder"
    target = bundle_path(client)
    current = existing_hash(target)
    expected = inputs_hash()
    if not force and current == expected:
        return "skipped"
    try:
        body = render_bundle(client)
    except SystemExit as e:
        return f"no_db ({e})"
    except Exception as e:
        return f"error:{type(e).__name__}:{e}"
    target.write_text(body)
    if verbose:
        print(f"[rebuilt] {target}")
    notify_swan_context_update(client)
    return "rebuilt"


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--client", help="Single client name (matches ~/.env.<Client>)")
    g.add_argument("--all", action="store_true",
                   help="Iterate every client with a ~/.env.<Name> file")
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if inputs_hash matches")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    clients = [args.client] if args.client else discover_clients()
    for c in clients:
        result = rebuild_one(c, force=args.force, verbose=args.verbose)
        if args.verbose or result.startswith("error") or result == "rebuilt":
            print(f"{c}: {result}")


if __name__ == "__main__":
    main()
