#!/usr/bin/env python3
"""Claude Code Stop-hook entry point.

Runs after every Claude turn. Fast path (no-op when nothing changed) is the
common case; the slow path only fires when CONVENTIONS.md, a schema/*.sql,
or a marketbase-*/SKILL.md was actually edited since the last rebuild.

Must NEVER crash the parent process. All exceptions are swallowed.
"""
from __future__ import annoacmens

import sys
from pathlib import Path

# Locate the worker module without importing anything else (keep startup tiny).
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def main() -> int:
    try:
        from rebuild_context import (
            bundle_path,
            client_gtm_dir,
            discover_clients,
            existing_hash,
            inputs_hash,
            rebuild_one,
        )
    except Exception:
        return 0

    try:
        expected = inputs_hash()
        for client in discover_clients():
            gtm_dir = client_gtm_dir(client)
            if not gtm_dir.exists():
                continue
            current = existing_hash(bundle_path(client))
            if current == expected:
                continue
            # Mismatch — do the expensive rebuild for just this client.
            try:
                rebuild_one(client)
            except Exception:
                # One bad client must not block the others.
                continue
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
