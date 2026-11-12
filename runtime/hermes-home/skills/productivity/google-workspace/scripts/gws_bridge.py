#!/usr/bin/env python3
"""Bridge between Hermes OAuth token and gws CLI.

Refreshes the token if expired, then executes gws with the valid access token.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure sibling modules (_hermes_home) are importable when run standalone.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _hermes_home import get_hermes_home

_AUTH_SCRIPTS = str(get_hermes_home() / "scripts")
if _AUTH_SCRIPTS not in sys.path:
    sys.path.insert(0, _AUTH_SCRIPTS)
from google_auth_store import load_credentials_for_path, safe_error



def get_token_path() -> Path:
    override = os.getenv("HERMES_GOOGLE_TOKEN_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    account = os.getenv("HERMES_GOOGLE_ACCOUNT", "").strip().lower()
    if account:
        if not account.replace("-", "").replace("_", "").isalnum():
            raise ValueError("HERMES_GOOGLE_ACCOUNT may contain only letters, numbers, '-' and '_'")
        return get_hermes_home() / f"google_token_{account}.json"
    return get_hermes_home() / "google_token.json"


def _normalize_authorized_user_payload(payload: dict) -> dict:
    normalized = dict(payload)
    if not normalized.get("type"):
        normalized["type"] = "authorized_user"
    return normalized


def refresh_token(token_data: dict) -> dict:
    """Ignore stale caller credentials; the locked store reloads the current grant."""
    return json.loads(load_credentials_for_path(get_token_path()).to_json())


def get_valid_token() -> str:
    return load_credentials_for_path(get_token_path()).token


def main():
    """Refresh token if needed, then exec gws with remaining args."""
    if len(sys.argv) < 2:
        print("Usage: gws_bridge.py <gws args...>", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "auth":
        raise RuntimeError("managed_account_requires_reviewed_private_renewal")
    access_token = get_valid_token()
    env = os.environ.copy()
    env.pop("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE", None)
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    env["GOOGLE_WORKSPACE_CLI_TOKEN"] = access_token

    result = subprocess.run(["gws"] + sys.argv[1:], env=env)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
