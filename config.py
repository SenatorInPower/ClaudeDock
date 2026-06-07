# -*- coding: utf-8 -*-
"""ClaudeDock — configuration loader.

No secrets live in the source. Settings are resolved in this order:

    1. environment variable  CLAUDEDOCK_<KEY>   (e.g. CLAUDEDOCK_WEB_PORT=9000)
    2. config.json           (next to this file, git-ignored)
    3. built-in default

To configure your own install: copy ``config.example.json`` to ``config.json``
and fill in the values you need. Everything is optional — with an empty config
the local dock still works fully (it only reads ~/.claude transcripts). Telegram
reports and remote web control simply stay disabled until you add their keys.
"""
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"

_DEFAULTS = {
    # Telegram (optional): reports, alerts, and Mini-App auth. Empty = disabled.
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    # Web controller (optional). Secret lives in the URL path; auto-generated and
    # saved back to config.json on first run if left empty.
    "web_url_secret": "",
    "web_host": "127.0.0.1",
    "web_port": 8765,
    # Optional password to control sessions remotely WITHOUT Telegram. Empty =
    # off (remote control then needs Telegram; the local PC always works).
    "web_password": "",
    # If a reverse proxy already gated the request (e.g. nginx Basic Auth), it can
    # vouch for it by sending header `X-ClaudeDock-Auth: <web_trust_key>` -> control
    # granted with no second in-app password. Keep this key secret.
    "web_trust_key": "",
    # Path to the `claude` executable, only if it is not on your PATH.
    "claude_bin": "",
    # Friendly names for the dock/web, matched as a lowercased substring of a
    # session's working directory. [["fragment", "Display Name"], ...]
    "project_names": [],
    # Projects shown in the "launch a session" picker. [{"name","path"}, ...]
    "launch_projects": [],
}

# Built-in display names for common system locations (kept generic / no PII).
DEFAULT_PROJECT_NAMES = [
    ["windows/system32", "system32"],
    ["users", "Home"],
]


def _load_file():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_FILE = _load_file()


def get(key, default=None):
    """Resolve one setting: env var > config.json > built-in default."""
    env = os.environ.get("CLAUDEDOCK_" + key.upper())
    if env not in (None, ""):
        if isinstance(_DEFAULTS.get(key), int):
            try:
                return int(env)
            except ValueError:
                pass
        return env
    val = _FILE.get(key)
    if val not in (None, "", [], {}):
        return val
    d = _DEFAULTS.get(key, default)
    return default if d is None else d


# Convenience shortcuts (read once at import).
BOT_TOKEN = get("telegram_bot_token")
CHAT_ID = get("telegram_chat_id")
WEB_HOST = get("web_host")
WEB_PORT = int(get("web_port"))


def claude_bin():
    """Best path to the `claude` CLI: config override, then PATH, then a guess."""
    return (get("claude_bin")
            or shutil.which("claude") or shutil.which("claude.cmd")
            or shutil.which("claude.bat") or "claude")


def project_names():
    """[[fragment, display_name], ...] — user list first, then generic defaults."""
    return list(get("project_names") or []) + DEFAULT_PROJECT_NAMES


def launch_projects():
    """[{"name","path"}, ...] — projects offered in the 'launch a session' picker."""
    return get("launch_projects") or []


def web_url_secret():
    """Secret embedded in the web URL path. If unset, generate one and persist it."""
    s = get("web_url_secret")
    if s:
        return s
    import secrets
    s = secrets.token_hex(16)
    data = _load_file()
    data["web_url_secret"] = s
    try:
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError:
        pass
    return s
