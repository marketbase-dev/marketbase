#!/usr/bin/env python3
"""marketbase-status-all-clients

Reports the schema migration status across every client's MarketBase. Read-only —
purely diagnostic. Use to spot drift when adding new migrations.

Discovers clients from ~/.env.* files. Connects to each, reads its
schema_migrations table, and prints a matrix.

Usage:
  python3 status_all_clients.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect, MIGRATIONS


def discover_clients() -> list[str]:
    """Returns client names parsed from ~/.env.<ClientName> files."""
    names = []
    for p in sorted(Path.home().glob(".env.*")):
        name = p.name[len(".env."):]
        if not name or name.startswith(".") or name.endswith(".bak"):
            continue
        names.append(name)
    return names


def fetch_applied(client: str) -> tuple[set[str] | None, str | None]:
    """Returns (applied_filenames, error_message)."""
    try:
        with connect(client) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema='public' AND table_name='schema_migrations'
                """)
                if not cur.fetchone():
                    return None, "no schema_migrations table"
                cur.execute("SELECT filename FROM schema_migrations")
                return {r[0] for r in cur.fetchall()}, None
    except Exception as e:
        return None, str(e).splitlines()[0][:80]


def main() -> None:
    clients = discover_clients()
    migration_names = [m.name for m in MIGRATIONS]

    if not clients:
        print("No clients found (no ~/.env.<ClientName> files).")
        return

    rows = []
    for c in clients:
        applied, err = fetch_applied(c)
        rows.append((c, applied, err))

    short_names = [re.match(r"^(\d+)_", n).group(1) for n in migration_names]
    client_w = max(8, max(len(c) for c in clients))

    print(f'{"Client":<{client_w}}  ' + "  ".join(short_names) + "   Notes")
    print("-" * (client_w + 4 + 4 * len(short_names) + 30))
    drift = False
    for client, applied, err in rows:
        if applied is None:
            cells = ["?  "] * len(short_names)
            note = err or "unknown"
        else:
            cells = [("✓  " if name in applied else "—  ") for name in migration_names]
            missing = [s for n, s in zip(migration_names, short_names) if n not in applied]
            if missing:
                drift = True
                note = f"missing: {', '.join(missing)}"
            else:
                note = "current"
        print(f"{client:<{client_w}}  " + "".join(cells) + " " + note)

    if drift:
        print("\n⚠ schema drift detected. Run marketbase-migrate-all-clients to propagate.")
    else:
        print("\nAll clients on the latest schema.")


if __name__ == "__main__":
    main()
