"""Secrets loader — .env first, OS env var overrides.

Single resolution path so adapters never reach into os.environ directly.
Call load_dotenv() once at CLI entry; getters cached after first read.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

_LOADED = False


def load_dotenv(path: Path | None = None) -> None:
    """Load .env at repo root if present. Idempotent. OS env wins on conflict."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    candidate = path or Path.cwd() / ".env"
    if not candidate.exists():
        return
    for line in candidate.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # OS env wins
        if key and key not in os.environ:
            os.environ[key] = val


@lru_cache(maxsize=None)
def get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def require(name: str) -> str:
    v = get(name)
    if not v:
        raise RuntimeError(
            f"missing required secret: {name}. "
            f"Set it in .env (see .env.example) or as an environment variable."
        )
    return v
