#!/usr/bin/env python3
"""marketbase-init-client — provision a new Neon project for a client.

Steps:
  1. Resolve the Impact 11 Neon org id (`<your-neon-org-id>`).
  2. Check if a project named `marketbase-<client>` already exists.
     - If yes, reuse it (skill is idempotent).
     - If no, create it.
  3. Find or create the client's own Infisical project (named exactly
     `<ClientName>`) and register it as a scope in
     `~/.claude/tools/infisical_credentials.json`.
  4. Store the connection URI there as `MARKETBASE_DB_CONNSTRING`. Nothing is
     written to `~/.env.<ClientName>`; a line left there by an older run is
     moved into the vault and removed from the file.
  5. Stamp a brand-new, empty database with its `db_identity` row.
  6. Apply all schema migrations in order.

Usage:
  python3 init_client.py <ClientName>          # e.g. Acme

Idempotent — safe to re-run. Each migration uses CREATE IF NOT EXISTS / DO
blocks so applying twice is a no-op.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import (env_path, apply_schema, connect, infisical_instance,
                 DB_KEY, INFISICAL_MAP, MIGRATIONS)

# The vault environment every scope in infisical_credentials.json uses.
INFISICAL_ENV = "dev"

ORG_ID_DEFAULT = "<your-neon-org-id>"   # Impact 11

# Compute size for a new instance: autoscaling from 0.25 to 1 compute unit.
# Neon bills awake time x size, and an instance spends most of its awake
# time waiting on the Python that calls it, so the floor is what the bill is
# made of. Without this flag a project can come up at a fixed, larger size.
DEFAULT_CU = "0.25-1"


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
        "--cu", DEFAULT_CU,
    )
    # `projects create` returns {project: {...}, connection_uris: [...], ...}
    if isinstance(res, dict) and "project" in res: return res
    sys.exit(f"unexpected response from `neonctl projects create`: {res!r}")


def get_connection_uri(project_id: str) -> str:
    res = run_neonctl("connection-string", "--project-id", project_id,
                      "--role-name", "neondb_owner",
                      json_out=False)
    return res.strip() if isinstance(res, str) else json.dumps(res)


def run_infisical(*args, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["infisical", *args], capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        sys.exit("the `infisical` CLI is not installed. The connection string is stored "
                 "in Infisical only; there is no env-file fallback.")
    except subprocess.TimeoutExpired:
        sys.exit(f"infisical {args[0]} timed out. Nothing was stored; re-run when it answers.")


def ensure_vault_project(client: str) -> dict:
    """The client's scope in infisical_credentials.json, creating the Infisical
    project and the scope when missing. Holding MARKETBASE_DB_CONNSTRING in its
    own project is what makes a client a MarketBase instance (lib.discover_instances),
    so an instance whose secret sits anywhere else is invisible to every
    all-instance job, migrations included."""
    inst = infisical_instance(client)
    if inst:
        print(f"  ✓ Infisical scope exists: {inst.get('projectName')} ({inst['projectId'][:8]}…)",
              flush=True)
        return inst

    r = run_infisical("sm", "projects", "list", "-o", "json", "--silent")
    if r.returncode != 0:
        sys.exit(f"could not list Infisical projects (are you logged in? `infisical login`):\n"
                 f"{r.stderr.strip()[-400:]}")
    projects = json.loads(r.stdout)
    if isinstance(projects, dict):
        projects = projects.get("projects") or []
    same = [p for p in projects if (p.get("name") or "") == client]
    if len(same) > 1:
        sys.exit(f"{len(same)} Infisical projects are named {client!r} "
                 f"({', '.join(p['id'][:8] + '…' for p in same)}). Add the right one to "
                 f"{INFISICAL_MAP} by hand; this script will not guess.")
    if same:
        proj = same[0]
        print(f"  ✓ Existing Infisical project found: {proj['id'][:8]}…", flush=True)
    else:
        r = run_infisical("sm", "projects", "create", "--name", client, "--description",
                          f"{client} MarketBase instance ({DB_KEY}). "
                          f"Created by init_client.py on {date.today()}.",
                          "-o", "json", "--silent")
        if r.returncode != 0:
            sys.exit(f"could not create the Infisical project {client!r}:\n"
                     f"{(r.stderr or r.stdout).strip()[-400:]}")
        out = json.loads(r.stdout)
        proj = out.get("project", out)
        print(f"  ✓ Created Infisical project: {proj['id'][:8]}…", flush=True)

    inst = {"projectId": proj["id"], "projectName": proj.get("name") or client,
            "env": INFISICAL_ENV,
            "note": f"{client}'s MarketBase instance ({DB_KEY}). "
                    f"Registered by init_client.py on {date.today()}."}
    raw = INFISICAL_MAP.read_text()
    cfg = json.loads(raw)
    cfg.setdefault("scopes", {})[client] = inst
    # One-space indent and the same trailing newline: the file's own format, so
    # the diff is the new scope and nothing else.
    INFISICAL_MAP.write_text(json.dumps(cfg, indent=1, ensure_ascii=False)
                             + ("\n" if raw.endswith("\n") else ""))
    print(f"  ✓ Registered scope {client!r} in {INFISICAL_MAP.name}", flush=True)
    return inst


def vault_get(inst: dict) -> str | None:
    r = run_infisical("secrets", "get", DB_KEY, "--projectId", inst["projectId"],
                      "--env", inst.get("env", INFISICAL_ENV), "--plain", "--silent")
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def vault_set(client: str, inst: dict, conn_url: str) -> None:
    """Store the connection string, then read it back: a set that did not land
    must stop the run here, not surface later as "not a MarketBase instance"."""
    r = run_infisical("secrets", "set", f"{DB_KEY}={conn_url}",
                      "--projectId", inst["projectId"],
                      "--env", inst.get("env", INFISICAL_ENV),
                      "--type", "shared", "--silent")
    if r.returncode != 0:
        sys.exit("could not store the connection string in Infisical:\n"
                 + (r.stderr or "").replace(conn_url, "<connection string>").strip()[-400:])
    if vault_get(inst) != conn_url:
        sys.exit("Infisical does not return the connection string that was just stored. "
                 "Nothing else was changed.")
    # secret_store caches a miss for the life of the process.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import secret_store
    secret_store._cache.pop(("scoped:" + client, DB_KEY), None)


def env_file_url(client: str) -> str | None:
    """A connection string an older run of this script left in ~/.env.<Client>."""
    p = env_path(client)
    if not p.exists():
        return None
    for ln in p.read_text().splitlines():
        s = ln.strip()
        if s.startswith(f"{DB_KEY}="):
            return s.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def drop_env_line(client: str) -> None:
    """Remove the connection-string line from ~/.env.<Client>, and the file with
    it when nothing else is left. Only called once the vault holds the same value."""
    p = env_path(client)
    keep = [ln for ln in p.read_text().splitlines()
            if not ln.strip().startswith(f"{DB_KEY}=")]
    if any(ln.strip() and not ln.strip().startswith("#") for ln in keep):
        p.write_text("\n".join(keep) + "\n")
        os.chmod(p, 0o600)
        print(f"  ✓ Removed {DB_KEY} from {p} (other keys kept)", flush=True)
    else:
        p.unlink()
        print(f"  ✓ Deleted {p} (it held nothing else)", flush=True)


def store_connection_string(client: str, project_id: str, fresh_uri: str = "") -> None:
    inst = ensure_vault_project(client)
    stored = vault_get(inst)
    legacy = env_file_url(client)
    if stored:
        print(f"  ✓ Infisical already holds {DB_KEY}", flush=True)
        if legacy and legacy != stored:
            # The vault is read first, so the file's copy is unused. Which one is
            # right is not this script's call.
            print(f"  ! {env_path(client)} holds a DIFFERENT {DB_KEY}. The vault's is the one "
                  f"in use; the file is left alone. Remove the stale one by hand.", flush=True)
            return
    else:
        conn_url = legacy or fresh_uri or get_connection_uri(project_id)
        vault_set(client, inst, conn_url)
        print(f"  ✓ Stored {DB_KEY} in Infisical project {inst.get('projectName')!r}"
              + ("  (moved from the env file)" if legacy else ""), flush=True)
    if legacy:
        drop_env_line(client)


def ensure_identity(client: str, project_id: str) -> None:
    """Stamp db_identity on a brand-new database. connect() refuses an unstamped
    one, so without this the first apply_schema of a new instance cannot run.
    Only an EMPTY database is stamped: an unstamped database that already has
    tables may be a clone of another instance, and saying which one it is stays
    a deliberate act (stamp_db_identity.py)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db_identity import read_identity, stamp
    conn = connect(client, _skip_identity=True)
    try:
        ident = read_identity(conn)
        if ident:
            print(f"  ✓ Database identity: {ident['instance']!r}/{ident['track']}", flush=True)
            return
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public'")
            n_tables = cur.fetchone()[0]
        conn.rollback()
        if n_tables:
            sys.exit(f"{client}: the database has {n_tables} table(s) and no db_identity row, so "
                     f"it is not a new instance. If it really is {client!r}, stamp it yourself:\n"
                     f"  python3 ~/.claude/tools/stamp_db_identity.py --client {client!r}")
        stamp(conn, client, "marketbase",
              note=f"Created by init_client.py on {date.today()} (Neon {project_id})")
        print(f"  ✓ Stamped the new database as {client!r}/marketbase", flush=True)
    finally:
        conn.close()


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
    fresh_uri = ""
    if proj:
        print(f"  ✓ Existing Neon project found: {proj.get('id')} ({proj.get('name')!r})",
              flush=True)
    else:
        print(f"  Creating Neon project '{canonical_name}' in org {args.org_id}…", flush=True)
        res = create_project(canonical_name, args.org_id, args.region)
        proj = res.get("project") or res
        print(f"  ✓ Created project: {proj.get('id')}", flush=True)
        for c in (res.get("connection_uris") or []):
            if c.get("connection_uri"):
                fresh_uri = c["connection_uri"]; break

    # 2. The connection string lives in the client's own Infisical project, never
    #    in ~/.env.<Client>. An instance whose secret is only in an env file is
    #    skipped by migrate_all_clients and every other all-instance job, so it
    #    silently falls behind on migrations.
    store_connection_string(client, proj["id"], fresh_uri)

    # 3. A new database needs its identity row before anything may connect
    ensure_identity(client, proj["id"])

    # 4. Apply schema migrations
    print(f"  Checking {len(MIGRATIONS)} schema migration(s)…", flush=True)
    result = apply_schema(client)
    for fn in result["applied"]:
        print(f"    ✓ applied  {fn}")
    for fn in result["skipped"]:
        print(f"    ⏭  already  {fn}")

    print(f"\n✓ marketbase-{client.lower()} is ready.")
    print(f"  Project ID: {proj.get('id')}")
    print(f"  Conn URL:   stored in Infisical project {client!r}  ({DB_KEY})")
    print(f"\nNext: marketbase-upload-leads to start importing your data.")


if __name__ == "__main__":
    main()
