#!/usr/bin/env python3
"""marketbase-register-processor

Register a processor (classifier, fetcher, reporter, orchestrator, enricher)
into the `processors` registry of a client's MarketBase. Accepts a YAML spec inline
or from a file. Idempotent: re-registering the same (name, version) updates the
YAML and re-extracts metadata. To bump the version, write a new YAML with a
different version field — the old row is preserved (set as `superseded_by`).

The YAML spec is the canonical source. The other fields (description,
processor_type, inputs, outputs) are extracted from top-level YAML keys for
queryability.

Expected YAML shape:

    name: demand_gen_persona_classifier
    version: "1.0"
    processor_type: classifier      # classifier | fetcher | reporter | orchestrator | enricher
    description: 4-tier engagement-type classification for B2B demand gen
    rule_changes: |
      vs prior version: raised posts_3mo threshold from 5 to 6
    inputs:
      fields_consulted: [employment_type, posts_3mo, avg_reactions_3mo, ...]
      depends_on_processors: [basic-creator-check@2026-05-acme-ai]
    outputs:
      writes_to_tables: [lead_qualifications]
      persona_values: [very active demand gen service provider, ...]
    decision_rule: |
      <natural language or pseudocode describing the actual decision>

Usage:
  python3 register_processor.py --client Acme-AI --yaml-file spec.yaml
  python3 register_processor.py --client Acme-AI --yaml-stdin           # read from stdin
"""
from __future__ import annoacmens

import argparse
import sys
from pathlib import Path

import yaml
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect


REQUIRED_TOP_LEVEL = ("name", "version", "processor_type", "yaml_spec_origin")
ALLOWED_PROCESSOR_TYPES = {"classifier", "fetcher", "reporter", "orchestrator", "enricher"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Register a processor (classifier/fetcher/reporter/orchestrator/enricher) into the MarketBase processors registry.")
    parser.add_argument("--client", required=True)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--yaml-file", help="Path to a YAML spec.")
    g.add_argument("--yaml-stdin", action="store_true", help="Read YAML spec from stdin.")
    parser.add_argument("--created-by", default=None)
    parser.add_argument("--no-supersede", action="store_true",
                        help="Don't mark prior versions of the same name as superseded.")
    args = parser.parse_args()

    if args.yaml_stdin:
        yaml_text = sys.stdin.read()
    else:
        yaml_text = Path(args.yaml_file).read_text()

    try:
        spec = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        sys.exit(f"Failed to parse YAML: {e}")

    if not isinstance(spec, dict):
        sys.exit("YAML must be a mapping at the top level.")

    # Required fields
    missing = [k for k in ("name", "version", "processor_type") if k not in spec]
    if missing:
        sys.exit(f"YAML missing required top-level keys: {missing}")
    if spec["processor_type"] not in ALLOWED_PROCESSOR_TYPES:
        sys.exit(f"processor_type must be one of {sorted(ALLOWED_PROCESSOR_TYPES)}, got {spec['processor_type']!r}")

    name         = str(spec["name"])
    version      = str(spec["version"])
    processor_type = str(spec["processor_type"])
    description  = spec.get("description")
    inputs       = spec.get("inputs")    # leave as dict; will be stored as jsonb
    outputs      = spec.get("outputs")
    rule_changes = spec.get("rule_changes")

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            # Find any prior row with same (name, version) — if so, UPSERT
            cur.execute("SELECT id FROM processors WHERE name = %s AND version = %s",
                        (name, version))
            existing = cur.fetchone()
            if existing:
                cur.execute("""
                    UPDATE processors SET
                      processor_type = %s,
                      description  = %s,
                      yaml_spec    = %s,
                      inputs       = %s,
                      outputs      = %s,
                      rule_changes = %s,
                      created_by   = COALESCE(%s, created_by)
                    WHERE id = %s
                """, (processor_type, description, yaml_text,
                      Jsonb(inputs) if inputs is not None else None,
                      Jsonb(outputs) if outputs is not None else None,
                      rule_changes, args.created_by, existing[0]))
                action = "updated"
                new_id = existing[0]
            else:
                # Insert new row
                cur.execute("""
                    INSERT INTO processors
                      (name, version, processor_type, description, yaml_spec,
                       inputs, outputs, rule_changes, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (name, version, processor_type, description, yaml_text,
                      Jsonb(inputs) if inputs is not None else None,
                      Jsonb(outputs) if outputs is not None else None,
                      rule_changes, args.created_by))
                new_id = cur.fetchone()[0]
                action = "inserted"

                # Mark older versions of the same name as superseded (unless --no-supersede)
                if not args.no_supersede:
                    cur.execute("""
                        UPDATE processors SET superseded_by = %s
                        WHERE name = %s AND id <> %s AND superseded_by IS NULL
                    """, (new_id, name, new_id))
                    if cur.rowcount:
                        print(f"  ↩  marked {cur.rowcount} prior version(s) of '{name}' as superseded")
        conn.commit()

    print(f"✓ {action} process {name}@{version}  (id={new_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
