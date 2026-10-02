"""Settings.

Everything is read from environment variables. For convenience, a file called `.env`
in the project folder is loaded first (one KEY=value per line), so API keys never have
to be typed into the code. `.env` is listed in .gitignore: it never gets committed.
See `.env.example` for every setting with an explanation.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path | None = None) -> None:
    """Read KEY=value lines from .env. Real environment variables always win.
    INPRO_NO_DOTENV=1 switches this off (the tests use it, so they never touch your real keys)."""
    if os.getenv("INPRO_NO_DOTENV") == "1" and path is None:
        return
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if val[:1] in "\"'" and val[-1:] == val[:1]:
            val = val[1:-1]
        elif " #" in val:                      # inline comment
            val = val.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = val


def env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def env_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in (env(name, default) or "").split(",") if x.strip()]
