"""Secret resolution for MarketBase.

Resolution order, first hit wins:

  1. Infisical  (recommended)  - if an Infisical credential is present
  2. Process environment
  3. A local .env file in the repo root

MarketBase never hard-codes credentials. If you find one in this repository,
please open a security issue.

## Using Infisical

Set these once, then every tool picks secrets up automatically:

    export INFISICAL_PROJECT_ID=...
    export INFISICAL_CLIENT_ID=...          # machine identity
    export INFISICAL_CLIENT_SECRET=...
    export INFISICAL_ENV=prod               # optional, default "prod"

Or run any tool under the Infisical CLI, which injects secrets as env vars and
needs nothing from this module:

    infisical run --env=prod -- python3 tools/qualify_cohort.py

## Multi-instance secrets

A secret can be scoped per MarketBase instance with an Infisical path.
`get("DATABASE_URL", instance="acme")` looks up `/acme/DATABASE_URL` first,
then falls back to `/DATABASE_URL`.
"""
from __future__ import annoacmens

import os
from functools import lru_cache
from pathlib import Path

_ENV_FILE_CACHE: dict[str, str] | None = None


def _load_env_file() -> dict[str, str]:
    global _ENV_FILE_CACHE
    if _ENV_FILE_CACHE is not None:
        return _ENV_FILE_CACHE
    out: dict[str, str] = {}
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break
    _ENV_FILE_CACHE = out
    return out


@lru_cache(maxsize=1)
def _infisical_client():
    """Return an authenticated Infisical client, or None if unconfigured."""
    cid = os.environ.get("INFISICAL_CLIENT_ID")
    csec = os.environ.get("INFISICAL_CLIENT_SECRET")
    token = os.environ.get("INFISICAL_TOKEN")
    if not (token or (cid and csec)):
        return None
    try:
        from infisical_sdk import InfisicalSDKClient
    except ImportError:
        return None
    try:
        client = InfisicalSDKClient(host=os.environ.get("INFISICAL_HOST", "https://app.infisical.com"))
        if token:
            client.auth.access_token(token)
        else:
            client.auth.universal_auth.login(client_id=cid, client_secret=csec)
        return client
    except Exception:
        return None


def _from_infisical(name: str, instance: str | None) -> str | None:
    client = _infisical_client()
    project = os.environ.get("INFISICAL_PROJECT_ID")
    if not client or not project:
        return None
    env = os.environ.get("INFISICAL_ENV", "prod")
    paths = [f"/{instance}", "/"] if instance else ["/"]
    for path in paths:
        try:
            res = client.secrets.get_secret_by_name(
                secret_name=name, project_id=project,
                environment_slug=env, secret_path=path,
            )
            value = getattr(getattr(res, "secret", res), "secretValue", None) or getattr(res, "secretValue", None)
            if value:
                return value
        except Exception:
            continue
    return None


def get(name: str, instance: str | None = None, default: str | None = None,
        required: bool = False) -> str | None:
    """Resolve a secret by name. See module docstring for the lookup order."""
    if instance:
        scoped = f"{instance.upper().replace('-', '_')}_{name}"
        for source in (os.environ, _load_env_file()):
            if source.get(scoped):
                return source[scoped]
    value = _from_infisical(name, instance)
    if value:
        return value
    value = os.environ.get(name) or _load_env_file().get(name)
    if value:
        return value
    if required:
        raise RuntimeError(
            f"Secret {name!r} is not set.\n"
            f"Provide it via Infisical (INFISICAL_PROJECT_ID + a machine identity), "
            f"an environment variable, or a .env file in the repo root.\n"
            f"See tools/lib/secrets.py for details."
        )
    return default


def get_list(name: str, instance: str | None = None) -> list[str]:
    """Resolve a comma-separated secret into a list. Used for API key pools.

    Example: APOLLO_API_KEYS="key1,key2,key3"
    """
    raw = get(name, instance=instance)
    return [p.strip() for p in raw.split(",") if p.strip()] if raw else []


def database_url(instance: str | None = None) -> str:
    """The Postgres connection string for a MarketBase instance."""
    for name in ("MARKETBASE_URL", "DATABASE_URL"):
        v = get(name, instance=instance)
        if v:
            return v
    raise RuntimeError(
        "No MarketBase database URL found.\n"
        "Set MARKETBASE_URL (or DATABASE_URL) via Infisical, the environment, or .env."
    )
