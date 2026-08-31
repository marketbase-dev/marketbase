#!/usr/bin/env python3
"""marketbase-init-client — provision a new Neon project for a client.

Steps:
  1. Resolve the Impact 11 Neon org id (`<your-neon-org-id>`).
  2. Check if a project named `marketbase-<client>` already exists.
     - If yes, reuse it (skill is idempotent).
     - If no, create it.
  3. Fetch the project's primary branch + connection URI.
  4. Write `~/.env.<ClientName>` with `GTM_DB_CONNSTRING=...`.
     (chmod 600 because secrets.)
  5. Apply all schema migrations in order.

Usage:
  python3 init_client.py <ClientName>          # e.g. Acme

Idempotent — safe to re-run. Each migration uses CREATE IF NOT EXISTS / DO
blocks so applying twice is a no-op.
"""
from __future__ import annoacmens

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import env_path, apply_schema, MIGRATIONS

ORG_ID_DEFAULT = "<your-neon-org-id>"   # Impact 11


def run_neonctl(*args, json_out=True) -> dict | list | str:
    cmd = ["npx", "-y", "neonctl", *args]
    if json_out: cmd += ["--output", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        sys.exit(f"neonctl {' '.join(args)} failed:\n{r.stderr}\n{r.stdout}")
    out = r.stdout.strip()
    if json_out and out:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out
    return out


def find_project(names: tuple[str, ...], org_id: str) -> dict | None:
    """Return the first project whose name matches any of `names` (case-insensitive)."""
    res = run_neonctl("projects", "list", "--org-id", org_id)
    projects = res.get("projects") if isinstance(res, dict) else res
    names_lower = {n.lower() for n in names}
    for p in (projects or []):
        if (p.get("name") or "").lower() in names_lower:
            return p
    return None


def create_project(name: str, org_id: str, region: str = "aws-us-east-1") -> dict:
    res = run_neonctl(
        "projects", "create",
        "--name", name, "--org-id", org_id, "--region-id", region,
    )
    # `projects create` returns {project: {...}, connection_uris: [...], ...}
    if isinstance(res, dict) and "project" in res: return res
    sys.exit(f"unexpected response from `neonctl projects create`: {res!r}")


def get_connection_uri(project_id: str) -> str:
    res = run_neonctl("connection-string", "--project-id", project_id,
                      "--role-name", "neondb_owner",
                      json_out=False)
    return res.strip() if isinstance(res, str) else json.dumps(res)


def write_env_file(client: str, conn_url: str):
    p = env_path(client)
    key = "GTM_DB_CONNSTRING"
    legacy_key = f"{client.upper()}_DATABASE_URL"
    lines = []
    if p.exists():
        for ln in p.read_text().splitlines():
            s = ln.strip()
            if s.startswith(f"{key}=") or s.startswith(f"{legacy_key}="):
                continue
            lines.append(ln)
    lines.append(f'{key}={conn_url}')
    p.write_text("\n".join(lines) + "\n")
    os.chmod(p, 0o600)


def main():
    ap = argparse.ArgumentParser(prog="marketbase-init-client")
    ap.add_argument("client", help="Client name (e.g. Acme). PascalCase recommended.")
    ap.add_argument("--org-id", default=ORG_ID_DEFAULT,
                    help=f"Neon org id (default: Impact 11 = {ORG_ID_DEFAULT}).")
    ap.add_argument("--region", default="aws-us-east-1")
    args = ap.parse_args()

    client = args.client
    # Accept either "marketbase-<client>" or just "<Client>" as the project name.
    canonical_name = f"marketbase-{client.lower()}"
    candidate_names = (canonical_name, client)

    print(f"→ Provisioning MarketBase DB for client: {client}", flush=True)

    # 1. Find or create the Neon project
    proj = find_project(candidate_names, args.org_id)
    if proj:
        print(f"  ✓ Existing Neon project found: {proj.get('id')} ({proj.get('name')!r})",
              flush=True)
    else:
        print(f"  Creating Neon project '{canonical_name}' in org {args.org_id}…", flush=True)
        res = create_project(canonical_name, args.org_id, args.region)
        proj = res.get("project") or res
        print(f"  ✓ Created project: {proj.get('id')}", flush=True)
        # New project: extract connection_uris if available
        conn_uri = ""
        for c in (res.get("connection_uris") or []):
            if c.get("connection_uri"):
                conn_uri = c["connection_uri"]; break
        if conn_uri:
            write_env_file(client, conn_uri)
            print(f"  ✓ Wrote {env_path(client)}", flush=True)

    # 2. Always make sure ~/.env.<Client> has a fresh connection URL
    if not env_path(client).exists():
        conn_uri = get_connection_uri(proj["id"])
        write_env_file(client, conn_uri)
        print(f"  ✓ Wrote {env_path(client)}", flush=True)
    else:
        print(f"  ✓ env file exists: {env_path(client)}", flush=True)

    # 3. Apply schema migrations
    print(f"  Checking {len(MIGRATIONS)} schema migration(s)…", flush=True)
    result = apply_schema(client)
    for fn in result["applied"]:
        print(f"    ✓ applied  {fn}")
    for fn in result["skipped"]:
        print(f"    ⏭  already  {fn}")

    print(f"\n✓ marketbase-{client.lower()} is ready.")
    print(f"  Project ID: {proj.get('id')}")
    print(f"  Conn URL:   stored in ~/.env.{client}  (GTM_DB_CONNSTRING)")
    print(f"\nNext: marketbase-upload-leads to start importing your data.")


if __name__ == "__main__":
    main()
