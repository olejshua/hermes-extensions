#!/usr/bin/env python3
"""Interactive, least-privilege renewal of the existing Stealth Google OAuth token.

Use a private terminal, or --callback-port with an SSH loopback tunnel.
Credentials, MFA codes and redirect URLs never need to enter a chat.
"""
import argparse
from contextlib import contextmanager
import fcntl
import stat
import getpass
import hmac
import http.server
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import Flow
from google_auth_store import generation, save_authorization_for_path, resume_authorization_for_path

HOME = Path.home() / '.hermes'
TOKEN = HOME / 'google_token_stealth.json'
CLIENT = HOME / 'google_client_secret.json'
REDIRECT = 'http://localhost:1'
EXPECTED_EMAIL = 'operator@secondary.example.invalid'


def validate_callback(url, state):
    parsed = urlparse(url.strip())
    params = parse_qs(parsed.query)
    if parsed.scheme != 'http' or parsed.hostname != 'localhost' or parsed.port != 1:
        raise ValueError('Unerwartete Weiterleitungsadresse')
    if params.get('state') != [state] or not params.get('code') or params.get('error'):
        raise ValueError('Google-Freigabe unvollständig oder abgelaufen')
    return url.strip()


@contextmanager
def renewal_session():
    """Exactly one private Stealth callback session, independent of refresh lock."""
    fd = os.open(TOKEN.with_name(TOKEN.name + '.renewal.lock'), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError('renewal_lock_owner_mismatch')
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('existing_private_renewal_session_active') from None
        yield
    finally:
        os.close(fd)


def main():
    with renewal_session():
        return interactive_main()


def interactive_main():
    parser = argparse.ArgumentParser(description='Stealth-Google-Zugriff sicher neu autorisieren')
    parser.add_argument('--resume-pending', action='store_true', help='Vorhandenen privaten Login-Kandidaten prüfen; keinen neuen Login starten')
    parser.add_argument('--timeout-seconds', type=int, default=3600, help='Einmalige lokale Sitzung: 300 bis 7200 Sekunden')
    parser.add_argument('--callback-port', type=int, help='Automatische Rückleitung über einen SSH-Loopback-Tunnel')
    args = parser.parse_args()
    if args.resume_pending:
        resume_authorization_for_path(TOKEN)
        print('OK: vorhandenen privaten Stealth-Kandidaten geprüft und sicher veröffentlicht.')
        return 0
    if not 300 <= args.timeout_seconds <= 7200:
        parser.error('Timeout muss zwischen 300 und 7200 Sekunden liegen.')
    if args.callback_port is not None and not 1024 <= args.callback_port <= 65535:
        parser.error('Callback-Port muss zwischen 1024 und 65535 liegen.')
    if args.callback_port is None and not sys.stdin.isatty():
        parser.exit(1, 'Nur in einem privaten interaktiven Terminal ausführen; keinen Code in Telegram senden.\n')
    if not CLIENT.is_file() or not TOKEN.is_file():
        parser.exit(1, 'Google-Client oder bestehender Stealth-Token fehlt.\n')
    old = json.loads(TOKEN.read_text(encoding='utf-8'))
    source_generation = generation(old)
    scopes = old.get('scopes') or []
    if not isinstance(scopes, list) or not scopes:
        parser.exit(1, 'Bisherige Berechtigungen nicht bestimmbar; keine automatische Ausweitung.\n')
    redirect = f'http://127.0.0.1:{args.callback_port}/' if args.callback_port else REDIRECT
    flow = Flow.from_client_secrets_file(
        str(CLIENT), scopes=scopes, redirect_uri=redirect,
        autogenerate_code_verifier=True,
    )
    url, state = flow.authorization_url(
        access_type='offline', prompt='select_account consent', login_hint=EXPECTED_EMAIL,
    )
    if args.callback_port:
        return run_loopback(flow, state, url, scopes, args.callback_port, source_generation, timeout=args.timeout_seconds)
    print('Diesen Link im eigenen Browser öffnen, das Stealth-Konto auswählen und freigeben:\n' + url)
    print('Die Fehlermeldung bei localhost:1 nach der Freigabe ist erwartbar.')
    callback = getpass.getpass('Vollständige Weiterleitungs-URL hier im privaten Terminal einfügen: ')
    try:
        callback = validate_callback(callback, state)
        code = parse_qs(urlparse(callback).query)['code'][0]
        os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
        # State was checked above; passing code avoids interpreting a local HTTP
        # redirect as an insecure token endpoint. The token exchange stays HTTPS.
        flow.fetch_token(code=code)
        save_verified_credentials(flow.credentials, scopes, source_generation)
    except Exception as exc:
        parser.exit(1, f'Google-Zugriff nicht bestätigt ({type(exc).__name__}); alter Token bleibt unverändert.\n')
    print('OK: Stealth-Gmail und Kalender mit dem richtigen Konto geprüft; Token sicher ersetzt.')


def save_verified_credentials(credentials, scopes, source_generation):
    # Only local checks precede durable capture. All Gmail/calendar/refresh
    # network proofs belong to the resumable shared-store sequence.
    granted_scopes = credentials.granted_scopes
    if granted_scopes is None:
        granted_scopes = credentials.scopes
    granted = set(granted_scopes or [])
    if not credentials.refresh_token or set(scopes) != granted:
        raise ValueError('authorization_scopes_or_refresh_invalid')
    save_authorization_for_path(TOKEN, credentials, source_generation=source_generation)


def run_loopback(flow, state, authorization_url, scopes, port, source_generation, timeout=3600):
    """Local-only callback. Start URL carries no code; nothing is logged."""
    outcome = {'done': False, 'ok': False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, text):
            page = ('<!doctype html><html lang="de"><meta charset="utf-8">'
                    '<meta name="referrer" content="no-referrer">'
                    '<title>Hermes Google-Freigabe</title><body><h1>' + text +
                    '</h1><p>Du kannst diesen Tab schließen.</p></body></html>').encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'")
            self.send_header('Content-Length', str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{port}':
                self.reply(403, 'Unerwartete Adresse.')
                return
            parsed = urlparse(self.path)
            if parsed.path == '/start' and not parsed.query:
                self.send_response(302)
                self.send_header('Location', authorization_url)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                return
            params = parse_qs(parsed.query)
            states = params.get('state', [])
            if parsed.path != '/' or len(states) != 1 or not hmac.compare_digest(states[0], state):
                self.reply(403, 'Die Freigabe gehört nicht zu dieser Sitzung.')
                return
            codes = params.get('code', [])
            issuer = params.get('iss', ['https://accounts.google.com'])
            if params.get('error') or len(codes) != 1 or issuer != ['https://accounts.google.com']:
                self.reply(400, 'Google-Freigabe nicht abgeschlossen. Bitte im Chat neu starten lassen.')
                outcome['done'] = True
                return
            try:
                os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
                flow.fetch_token(code=codes[0])
                save_verified_credentials(flow.credentials, scopes, source_generation)
            except Exception as exc:
                print(f'Google-Zugriff nicht bestätigt ({type(exc).__name__}); alter Token bleibt unverändert.', flush=True)
                self.reply(400, 'Zugriff nicht bestätigt. Der bestehende Zugang wurde nicht ersetzt.')
            else:
                outcome['ok'] = True
                print('OK: Stealth-Gmail und Kalender mit dem richtigen Konto geprüft; Token sicher ersetzt.', flush=True)
                self.reply(200, 'Stealth-Mail und Kalender sind wieder verbunden.')
            outcome['done'] = True

    with http.server.HTTPServer(('127.0.0.1', port), Handler) as server:
        server.timeout = 1
        print(f'BROWSER_START=http://127.0.0.1:{port}/start', flush=True)
        deadline = time.monotonic() + timeout
        while not outcome['done'] and time.monotonic() < deadline:
            server.handle_request()
    if not outcome['done']:
        print('Lokale Freigabesitzung abgelaufen; bestehenden Token nicht ersetzt.', flush=True)
    return 0 if outcome['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
