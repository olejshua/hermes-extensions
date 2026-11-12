#!/usr/bin/env python3
"""Google Workspace OAuth2 setup for Hermes Agent.

Fully non-interactive — designed to be driven by the agent via terminal commands.
The agent mediates between this script and the user (works on CLI, Telegram, Discord, etc.)

Commands:
  setup.py --check                          # Is auth valid? Exit 0 = yes, 1 = no
  setup.py --client-secret /path/to.json    # Store OAuth client credentials
  setup.py --auth-url                       # Print the OAuth URL for user to visit
  setup.py --auth-code CODE                 # Exchange auth code for token
  setup.py --revoke                         # Revoke and delete stored token
  setup.py --install-deps                   # Install Python dependencies only

Agent workflow:
  1. Run --check. If exit 0, auth is good — skip setup.
  2. Ask user for client_secret.json path. Run --client-secret PATH.
  3. Run --auth-url. Send the printed URL to the user.
  4. User opens URL, authorizes, gets redirected to a page with a code.
  5. User pastes the code. Agent runs --auth-code CODE.
  6. Run --check to verify. Done.
"""

from __future__ import annotations  # allow PEP 604 `X | None` on Python 3.9+

import argparse
import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import version as _distribution_version
from pathlib import Path

# Ensure sibling modules (_hermes_home) are importable when run standalone.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _hermes_home import display_hermes_home, get_hermes_home

_AUTH_SCRIPTS = str(get_hermes_home() / "scripts")
if _AUTH_SCRIPTS not in sys.path:
    sys.path.insert(0, _AUTH_SCRIPTS)
from google_auth_store import load_credentials_for_path, safe_error, AuthFailure


HERMES_HOME = get_hermes_home()


def _resolve_token_path() -> Path:
    """Select a default or named Google account for setup and refresh."""
    override = os.getenv("HERMES_GOOGLE_TOKEN_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    account = os.getenv("HERMES_GOOGLE_ACCOUNT", "").strip().lower()
    if account:
        if not account.replace("-", "").replace("_", "").isalnum():
            raise ValueError("HERMES_GOOGLE_ACCOUNT may contain only letters, numbers, '-' and '_'")
        return HERMES_HOME / f"google_token_{account}.json"
    return HERMES_HOME / "google_token.json"


TOKEN_PATH = _resolve_token_path()
CLIENT_SECRET_PATH = HERMES_HOME / "google_client_secret.json"
PENDING_AUTH_PATH = HERMES_HOME / "google_oauth_pending.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]

# Exact pins: keep in sync with pyproject.toml [project.optional-dependencies].google
# and tools/lazy_deps.py LAZY_DEPS['skill.google_workspace'].
# Pinning all protects against version drift and ensures the security floors
# (httplib2 GHSA-j5g9-f88f-gfj3, stale pyasn1/google-auth) are honoured
# regardless of install path.
REQUIRED_PACKAGES = [
    "google-api-python-client==2.194.0",
    "google-auth==2.55.1",
    "google-auth-oauthlib==1.3.1",
    "google-auth-httplib2==0.3.1",
    # GHSA-j5g9-f88f-gfj3 — Decompression Bomb DoS via unbounded gzip/deflate
    "httplib2==0.32.0",
    "pyasn1==0.6.4",
]

# OAuth redirect for "out of band" manual code copy flow.
# Google deprecated OOB, so we use a localhost redirect and tell the user to
# copy the code from the browser's URL bar (or the page body).
REDIRECT_URI = "http://localhost:1"


def _normalize_authorized_user_payload(payload: dict) -> dict:
    normalized = dict(payload)
    if not normalized.get("type"):
        normalized["type"] = "authorized_user"
    return normalized


def _load_token_payload(path: Path = TOKEN_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _missing_scopes_from_payload(payload: dict) -> list[str]:
    raw = payload.get("scopes") or payload.get("scope")
    if not raw:
        return []
    granted = {s.strip() for s in (raw.split() if isinstance(raw, str) else raw) if s.strip()}
    return sorted(scope for scope in SCOPES if scope not in granted)


def _format_missing_scopes(missing_scopes: list[str]) -> str:
    bullets = "\n".join(f"  - {scope}" for scope in missing_scopes)
    return (
        "Token is valid but missing required Google Workspace scopes:\n"
        f"{bullets}\n"
        "Run the Google Workspace setup again from this same Hermes profile to refresh consent."
    )


def _missing_required_packages() -> list[str]:
    """Return exact requirements absent or stale in this interpreter.

    All REQUIRED_PACKAGES entries are exact ``name==version`` pins, so a
    direct version comparison is sufficient — no ``packaging`` dependency
    needed in this standalone script.
    """
    missing = []
    for spec in REQUIRED_PACKAGES:
        name, _, wanted = spec.partition("==")
        try:
            if _distribution_version(name) != wanted:
                missing.append(spec)
        except Exception:
            missing.append(spec)
    return missing


def install_deps():
    """Install missing or stale Google API packages. Returns True on success."""
    missing = _missing_required_packages()
    if not missing:
        print("Dependencies already installed.")
        return True

    print("Installing Google API dependencies...")

    # First choice: pip in the current interpreter. Works for most installs.
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + missing,
            stdout=subprocess.DEVNULL,
        )
        remaining = _missing_required_packages()
        if remaining:
            print(f"ERROR: Dependencies remain stale after pip install: {' '.join(remaining)}")
            return False
        print("Dependencies installed.")
        return True
    except subprocess.CalledProcessError as e:
        pip_error = e

    # Fallback: the interpreter has no pip (the Hermes Docker image's venv is
    # built with `uv sync`, which does not bootstrap pip). `uv pip install
    # --python <interpreter>` installs into that exact interpreter without
    # needing pip present. Targeting sys.executable keeps us on the venv the
    # script is actually running under, rather than guessing.
    uv = shutil.which("uv")
    if uv:
        try:
            subprocess.check_call(
                [uv, "pip", "install", "--python", sys.executable, "--quiet"]
                + missing,
                stdout=subprocess.DEVNULL,
            )
            remaining = _missing_required_packages()
            if remaining:
                print(f"ERROR: Dependencies remain stale after uv install: {' '.join(remaining)}")
                return False
            print("Dependencies installed.")
            return True
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to install dependencies via uv: {e}")
            print(f"Manually: {uv} pip install --python {sys.executable} {' '.join(REQUIRED_PACKAGES)}")
            return False

    print(f"ERROR: Failed to install dependencies: {pip_error}")
    print(
        "On environments without pip (e.g. Nix, or the Hermes Docker image's "
        "uv-managed venv), install the optional extra instead:"
    )
    print("  hermes setup")
    print(f"Or manually: {sys.executable} -m pip install {' '.join(REQUIRED_PACKAGES)}")
    return False


def _ensure_deps():
    """Check exact dependency versions, install if stale, exit on failure."""
    if _missing_required_packages() and not install_deps():
        sys.exit(1)


def check_auth_live():
    """Keep the canonical StoredCredentials for every live Calendar refresh."""
    _ensure_deps()
    try:
        from googleapiclient.discovery import build
        creds = load_credentials_for_path(TOKEN_PATH)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        service.calendarList().list(maxResults=1).execute(num_retries=0)
        print("LIVE_CHECK_OK: existing_account_calendar_verified")
        return True
    except Exception as exc:
        print("LIVE_CHECK_FAILED: " + safe_error(exc).code)
        return False


def check_auth(quiet: bool = False):
    """Read-only account profile and shared refresh path; no exception contents."""
    _ensure_deps()
    try:
        load_credentials_for_path(TOKEN_PATH)
    except Exception as exc:
        print("AUTH_UNAVAILABLE: " + safe_error(exc).code)
        return False
    if not quiet:
        print("AUTHENTICATED: existing_account_profile_verified")
    return True


def store_client_secret(path: str):
    """Managed-account auth changes use the reviewed private loopback only."""
    raise AuthFailure("managed_account_requires_reviewed_private_renewal")


def _save_pending_auth(*, state: str, code_verifier: str):
    """Persist the OAuth session bits needed for a later token exchange."""
    PENDING_AUTH_PATH.write_text(
        json.dumps(
            {
                "state": state,
                "code_verifier": code_verifier,
                "redirect_uri": REDIRECT_URI,
            },
            indent=2,
        ), encoding="utf-8"
    )


def _load_pending_auth() -> dict:
    """Load the pending OAuth session created by get_auth_url()."""
    if not PENDING_AUTH_PATH.exists():
        print("ERROR: No pending OAuth session found. Run --auth-url first.")
        sys.exit(1)

    try:
        data = json.loads(PENDING_AUTH_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: Could not read pending OAuth session: {e}")
        print("Run --auth-url again to start a fresh OAuth session.")
        sys.exit(1)

    if not data.get("state") or not data.get("code_verifier"):
        print("ERROR: Pending OAuth session is missing PKCE data.")
        print("Run --auth-url again to start a fresh OAuth session.")
        sys.exit(1)

    return data


def _extract_code_and_state(code_or_url: str) -> tuple[str, str | None]:
    """Accept either a raw auth code or the full redirect URL pasted by the user."""
    if not code_or_url.startswith("http"):
        return code_or_url, None

    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(code_or_url)
    params = parse_qs(parsed.query)
    if "code" not in params:
        print("ERROR: No 'code' parameter found in URL.")
        sys.exit(1)

    state = params.get("state", [None])[0]
    return params["code"][0], state


def get_auth_url():
    """Managed-account auth changes use the reviewed private loopback only."""
    raise AuthFailure("managed_account_requires_reviewed_private_renewal")


def exchange_auth_code(code: str):
    """Managed-account auth changes use the reviewed private loopback only."""
    raise AuthFailure("managed_account_requires_reviewed_private_renewal")


def revoke():
    """Managed-account auth changes use the reviewed private loopback only."""
    raise AuthFailure("managed_account_requires_reviewed_private_renewal")


def main():
    parser = argparse.ArgumentParser(description="Google Workspace OAuth setup for Hermes")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Check if auth is valid (exit 0=yes, 1=no)")
    group.add_argument("--check-live", action="store_true", help="Check auth with a real API call (detects disabled_client)")
    group.add_argument("--client-secret", metavar="PATH", help="Store OAuth client_secret.json")
    group.add_argument("--auth-url", action="store_true", help="Print OAuth URL for user to visit")
    group.add_argument("--auth-code", metavar="CODE", help="Exchange auth code for token")
    group.add_argument("--revoke", action="store_true", help="Revoke and delete stored token")
    group.add_argument("--install-deps", action="store_true", help="Install Python dependencies")
    args = parser.parse_args()

    if args.check:
        sys.exit(0 if check_auth() else 1)
    if getattr(args, "check_live", False):
        sys.exit(0 if check_auth_live() else 1)
    elif args.client_secret:
        store_client_secret(args.client_secret)
    elif args.auth_url:
        get_auth_url()
    elif args.auth_code:
        exchange_auth_code(args.auth_code)
    elif args.revoke:
        revoke()
    elif args.install_deps:
        sys.exit(0 if install_deps() else 1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("AUTH_UNAVAILABLE: " + safe_error(exc).code, file=sys.stderr)
        raise SystemExit(1) from None
