#!/usr/bin/env python3
"""marketbase-show-process

Fetch a process from the registry and print its YAML spec. By default returns
the latest non-superseded version of the named process; pass --version to
target a specific historical version.

Usage:
  python3 show_process.py --client Acme-AI --name demand_gen_persona_classifier
  python3 show_process.py --client Acme-AI --name demand_gen_persona_classifier --version 2026-05-acme-ai
  python3 show_process.py --client Acme-AI --name demand_gen_persona_classifier --metadata-only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect


def main() -> int:
    parser = argparse.ArgumentParser(description="Show a process's YAML spec.")
    parser.add_argument("--client", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", help="Specific version. Default = latest non-superseded.")
    parser.add_argument("--metadata-only", action="store_true",
                        help="Show only metadata (skip yaml_spec body).")
    args = parser.parse_args()

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            if args.version:
                cur.execute("""
                    SELECT name, version, processor_type, description, yaml_spec,
                           rule_changes, created_at, created_by, superseded_by IS NOT NULL AS superseded
                    FROM processors WHERE name = %s AND version = %s
                """, (args.name, args.version))
            else:
                cur.execute("""
                    SELECT name, version, processor_type, description, yaml_spec,
                           rule_changes, created_at, created_by, superseded_by IS NOT NULL AS superseded
                    FROM processors
                    WHERE name = %s AND superseded_by IS NULL
                    ORDER BY created_at DESC LIMIT 1
                """, (args.name,))
            row = cur.fetchone()
            if not row:
                sys.exit(f"No process found for name={args.name!r}"
                         f"{' version='+repr(args.version) if args.version else ''}.")

    name, version, ptype, desc, yaml_spec, changes, created, by, superseded = row
    print(f"# {name}@{version}")
    print(f"type:        {ptype}")
    print(f"description: {desc or '(none)'}")
    print(f"created:     {created:%Y-%m-%d %H:%M} by {by or '(unknown)'}")
    if superseded:
        print(f"⚠  superseded by a newer version")
    if changes:
        print(f"\nrule_changes:\n  {changes}")
    if not args.metadata_only:
        print(f"\n--- yaml_spec ---")
        print(yaml_spec.rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
