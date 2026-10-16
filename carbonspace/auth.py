"""Credential and config storage.

Two credentials are used:
- API key (`email|key`): read-only. Long-lived. Used for check/map/report/compare.
- Access token (JWT from the web app): required for writes. Expires after ~24 h.

Both are stored per user in Windows Credential Manager via `keyring`, with a
plain-text fallback in %APPDATA%/CarbonSpaceSync/config.json. Environment
variables CARBONSPACE_API_KEY / CARBONSPACE_TOKEN take precedence.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SERVICE = "CarbonSpaceSync"
ENV_KEY = "CARBONSPACE_API_KEY"
ENV_TOKEN = "CARBONSPACE_TOKEN"

# Snippet the user runs in the browser console on public.carbonspace.earth.
TOKEN_SNIPPET = (
    "copy(await [...document.querySelectorAll('*')].find(e=>e.__vue_app__)"
    ".__vue_app__.config.globalProperties.$auth0.getAccessTokenSilently())"
)

DEFAULT_CONFIG = {
    "library_project_id": 1302,
    "search_project_ids": [1302, 155, 154, 156],
    "template_calculation_id": None,
    "default_group": "Revit import",
    # Schedule title keyword -> CarbonSpace calculation group (first match wins).
    "group_by_schedule": {
        "wall": "Facade opaque",
        "curtain": "Facade transparent",
        "window": "Facade transparent",
        "roof": "Roofs",
        "floor": "Floors",
        "ceiling": "Ceilings and raised floors",
        "stair": "Stairs and ramps",
        "column": "Columns",
        "framing": "Beams",
        "beam": "Beams",
        "foundation": "Foundation",
    },
    "columns": {},
}


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "CarbonSpaceSync"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict:
    """User config merged over defaults. A cs_sync.json in the working
    directory (project settings, no secrets) overrides both."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for p in (config_path(), Path.cwd() / "cs_sync.json"):
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            # Secrets are read via get_secret(), never via the merged config.
            data.pop("api_key", None)
            data.pop("token", None)
            cfg.update(data)
    return cfg


def _save_plain(name: str, value: str | None) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if value is None:
        data.pop(name, None)
    else:
        data[name] = value
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _keyring():
    try:
        import keyring  # noqa: PLC0415

        keyring.get_keyring()
        return keyring
    except Exception:
        return None


def store_secret(name: str, value: str) -> str:
    """Store a secret; returns a description of where it went."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, name, value)
            _save_plain(name, None)
            return "Windows Credential Manager"
        except Exception:
            pass
    _save_plain(name, value)
    return f"{config_path()} (PLAIN TEXT - anyone with access to this file can use it)"


def get_secret(name: str, env: str) -> str | None:
    if os.environ.get(env):
        return os.environ[env].strip()
    kr = _keyring()
    if kr is not None:
        try:
            v = kr.get_password(SERVICE, name)
            if v:
                return v
        except Exception:
            pass
    p = config_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8")).get(name)
    return None


def get_api_key() -> str | None:
    return get_secret("api_key", ENV_KEY)


def get_token() -> str | None:
    return get_secret("token", ENV_TOKEN)


def jwt_claims(token: str) -> dict | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def token_seconds_left(token: str | None) -> float:
    claims = jwt_claims(token) if token else None
    if not claims or "exp" not in claims:
        return 0
    return claims["exp"] - time.time()


def read_clipboard() -> str:
    if sys.platform == "win32":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, timeout=20,
        )
        return (out.stdout or "").strip()
    try:
        import tkinter  # noqa: PLC0415

        root = tkinter.Tk()
        root.withdraw()
        text = root.clipboard_get()
        root.destroy()
        return text.strip()
    except Exception:
        return ""


def mask(secret: str | None) -> str:
    """Show a secret safely in logs: 'you@…|ab…'."""
    if not secret:
        return "<none>"
    if "|" in secret:
        user, key = secret.split("|", 1)
        return f"{user}|{key[:2]}…"
    return secret[:6] + "…"
