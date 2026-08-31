"""Named MarketBase instances.

Most people run ONE MarketBase: their own company's GTM context. Agencies,
consultancies, and anyone managing GTM for multiple organizations run several.
Both are first-class.

An instance is just a name mapped to a Postgres database. Nothing else differs:
same schema, same tools, same commands.

Resolution order for "which instance am I talking to":

  1. --instance <name> on the command line
  2. $MARKETBASE_INSTANCE
  3. "default_instance" in the config file
  4. the literal name "default"

Config lives at ~/.marketbase/instances.json (override with $MARKETBASE_CONFIG).
It holds names and metadata, never credentials:

    {
      "default_instance": "acme",
      "instances": {
        "acme":      {"description": "Acme Corp", "created": "2026-09-01"},
        "northwind": {"description": "Northwind Ltd"}
      }
    }

Credentials resolve separately through lib/secrets.py, which checks the
instance-scoped name first and then the global one:

    ACME_MARKETBASE_URL   ->  MARKETBASE_URL
    Infisical /acme/...   ->  Infisical /...

That means one shared Infisical project can hold every instance's connection
string without any of them colliding.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_VALID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def config_path() -> Path:
    return Path(os.environ.get("MARKETBASE_CONFIG",
                               Path.home() / ".marketbase" / "instances.json"))


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {"default_instance": None, "instances": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{p} is not valid JSON: {e}") from e
    d.setdefault("instances", {})
    d.setdefault("default_instance", None)
    return d


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    p.chmod(0o600)


def normalize(name: str) -> str:
    """Instance names are lowercase, digits, hyphen, underscore."""
    n = name.strip().lower().replace(" ", "-")
    if not _VALID.match(n):
        raise ValueError(
            f"Invalid instance name {name!r}. Use lowercase letters, digits, "
            f"hyphens, and underscores; start with a letter or digit."
        )
    return n


def resolve(explicit: str | None = None) -> str:
    """Which instance are we operating on? See module docstring for the order."""
    if explicit:
        return normalize(explicit)
    if os.environ.get("MARKETBASE_INSTANCE"):
        return normalize(os.environ["MARKETBASE_INSTANCE"])
    cfg = load_config()
    if cfg.get("default_instance"):
        return normalize(cfg["default_instance"])
    return "default"


def register(name: str, description: str = "", make_default: bool = False) -> str:
    name = normalize(name)
    cfg = load_config()
    entry = cfg["instances"].get(name, {})
    if description:
        entry["description"] = description
    entry.setdefault("created", __import__("datetime").date.today().isoformat())
    cfg["instances"][name] = entry
    if make_default or not cfg.get("default_instance"):
        cfg["default_instance"] = name
    save_config(cfg)
    return name


def listing() -> list[dict]:
    cfg = load_config()
    default = cfg.get("default_instance")
    return [
        {"name": n, "default": n == default, **meta}
        for n, meta in sorted(cfg["instances"].items())
    ]


def add_argument(parser) -> None:
    """Attach the standard --instance flag to an argparse parser."""
    parser.add_argument(
        "--instance", default=None,
        help="MarketBase instance to operate on. Defaults to "
             "$MARKETBASE_INSTANCE, then the configured default.",
    )
