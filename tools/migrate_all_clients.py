#!/usr/bin/env python3
"""marketbase-migrate-all-clients

Propagates schema migrations to every client's MarketBase in one shot. Iterates
over discovered ~/.env.<ClientName> files and runs apply_schema() on each.

Safe to re-run — apply_schema() skips already-applied migrations via the
schema_migrations tracking table.

Usage:
  python3 migrate_all_clients.py            # apply to all clients
  python3 migrate_all_clients.py --dry-run  # show what would happen, don't write
"""
from __future__ import annoacmens

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import apply_schema, applied_migrations, MIGRATIONS


def discover_clients() -> list[str]:
    names = []
    for p in sorted(Path.home().glob(".env.*")):
        name = p.name[len(".env."):]
        if not name or name.startswith(".") or name.endswith(".bak"):
            continue
        names.append(name)
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="Propagate schema migrations to every client MarketBase.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be applied, but do not write.")
    args = parser.parse_args()

    clients = discover_clients()
    if not clients:
        print("No clients found.")
        return 0

    print(f"Found {len(clients)} client(s): {', '.join(clients)}")
    print(f"Latest migration: {MIGRATIONS[-1].name}")
    print()

    any_failed = False
    for client in clients:
        print(f"→ {client}")
        try:
            if args.dry_run:
                already = applied_migrations(client)
                to_apply = [m.name for m in MIGRATIONS if m.name not in already]
                if to_apply:
                    print(f"  would apply: {', '.join(to_apply)}")
                else:
                    print(f"  ✓ up to date ({len(already)} migrations applied)")
            else:
                result = apply_schema(client)
                if result["applied"]:
                    for fn in result["applied"]:
                        print(f"  ✓ applied  {fn}")
                else:
                    print(f"  ✓ up to date ({len(result['skipped'])} migrations skipped)")
        except Exception as e:
            any_failed = True
            print(f"  ✗ FAILED: {e}")
            print(f"     → continuing with remaining clients; re-run after investigating")
        print()

    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
