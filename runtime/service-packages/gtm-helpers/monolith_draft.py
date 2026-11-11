#!/usr/bin/env python3
"""Submit extracted evidence, never a transcript or approved task. No automatic write retry."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid

DEFAULT_API = 'https://project.example.invalid/functions/v1/make-server-ad69ef10'

def canonical_hash(payload):
    source = {key: value for key, value in payload.items() if key != 'requestId'}
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

def credential(source="codex"):
    if source == "hermes":
        path = Path.home()/'.hermes/.env'
        if not path.exists():
            raise RuntimeError('Vorhandene Hermes-Konfiguration fehlt; Monolith bleibt offen.')
        for line in path.read_text().splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip() == 'MONOLITH_AGENT_TOKEN' and value.strip():
                return value.strip().strip('"').strip("'")
        raise RuntimeError('Allgemeines Hermes-Agent-Token fehlt; Monolith bleibt offen.')
    token = os.environ.get('MONOLITH_AGENT_TOKEN')
    if token:
        return token
    result = subprocess.run(['security', 'find-generic-password', '-a', 'codex', '-s', 'monolith-agent-token', '-w'], capture_output=True, text=True)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError('Vorhandenes allgemeines Monolith-Agent-Token fehlt: Keychain service monolith-agent-token, account codex; optional MONOLITH_AGENT_TOKEN. Kein neues Token erzeugt, kein Worker-Token verwendet.')
    return result.stdout.strip()

def validate(payload):
    for key in ('meetingId', 'title', 'occurredAt', 'version', 'notionUrl', 'transcriptHash', 'actionItems'):
        if key not in payload:
            raise ValueError(f'Pflichtfeld fehlt: {key}')
    if not isinstance(payload['version'], int) or payload['version'] < 1:
        raise ValueError('Explizite positive Quellversion erforderlich')
    if not isinstance(payload['actionItems'], list) or not 1 <= len(payload['actionItems']) <= 80:
        raise ValueError('1 bis 80 bereits extrahierte Schritte erforderlich')
    for item in payload['actionItems']:
        for key in ('key', 'title', 'evidence', 'assignee', 'dueDate', 'deadlineBasis', 'estimatedMinutes'):
            if key not in item:
                raise ValueError(f'Schritt-Pflichtfeld fehlt: {key}; unbekannte Werte ausdrücklich null')
        if item['assignee'] not in ('ole', 'other', 'unclear'):
            raise ValueError('Zuständigkeit muss ole, other oder unclear sein')
        if not isinstance(item['evidence'], str) or not item['evidence'].strip():
            raise ValueError('Originalbeleg erforderlich')
    allowed = {'requestId','meetingId','title','company','category','occurredAt','version','notionUrl','sourceUrl','transcriptHash','actionItems','targetDate','preferences'}
    if set(payload) - allowed:
        raise ValueError('Unbekannte Felder: kein Rohtranskript an Monolith senden')
    categories = ('acribic', 'stealth', 'personal', 'unclear')
    if 'category' in payload and payload['category'] not in categories:
        raise ValueError('Meeting-Kategorie ungültig')
    if payload.get('company') is None and 'category' not in payload:
        raise ValueError('Allgemeines Meeting braucht eine ausdrückliche Kategorie')
    payload['company'] = payload.get('company')
    if payload['company'] is not None:
        payload['company'] = payload['company'].strip()
    for item in payload['actionItems']:
        if 'category' in item and item['category'] not in categories:
            raise ValueError('Schritt-Kategorie ungültig')
        if set(item) - {'key','title','evidence','category','assignee','assigneeName','dueDate','deadlineBasis','estimatedMinutes'}:
            raise ValueError('Unbekanntes Schritt-Feld')
    for key in ('meetingId', 'title'):
        payload[key] = payload[key].strip()
    payload['occurredAt'] = datetime.fromisoformat(payload['occurredAt'].replace('Z', '+00:00')).astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    payload['transcriptHash'] = payload['transcriptHash'].lower()
    for key in ('notionUrl', 'sourceUrl'):
        raw = payload.get(key)
        if raw:
            parsed = urllib.parse.urlsplit(raw.strip())
            if parsed.scheme != 'https' or parsed.username or parsed.password:
                raise ValueError('HTTPS-Link ohne Zugangsdaten erforderlich')
            host = parsed.hostname.lower()
            netloc = host + (f':{parsed.port}' if parsed.port and parsed.port != 443 else '')
            payload[key] = urllib.parse.urlunsplit(('https', netloc, parsed.path or '/', parsed.query, parsed.fragment))
        else:
            payload[key] = None
    for item in payload['actionItems']:
        for key in ('key', 'title', 'evidence'):
            item[key] = item[key].strip()
        item['assigneeName'] = item.get('assigneeName')
        if item['assigneeName'] is not None:
            item['assigneeName'] = item['assigneeName'].strip()
        if item['deadlineBasis'] is not None:
            item['deadlineBasis'] = item['deadlineBasis'].strip()
    if 'preferences' in payload:
        payload['preferences']['weekdays'] = sorted(payload['preferences']['weekdays'])
    payload.setdefault('requestId', str(uuid.uuid4()))
    return payload

def request(base, token, path, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(base + path, data=data, headers={'Content-Type': 'application/json', 'X-Monolith-Agent-Token': token}, method='GET' if payload is None else 'POST')
    with urllib.request.urlopen(req, timeout=45) as response:
        value = json.load(response)
    if value.get('success') is not True:
        raise RuntimeError('Monolith hat den Vorgang nicht bestätigt')
    return value['data']

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credential-source', choices=('codex', 'hermes'), default='codex')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--readback', action='store_true')
    parser.add_argument('--receipt-dir', default=str(Path.home()/'.cache/acribic/meetings'))
    args = parser.parse_args()
    payload = validate(json.load(sys.stdin))
    fingerprint = canonical_hash(payload)
    print(json.dumps({'mode': 'readback' if args.readback else 'write' if args.write else 'dry-run', 'items': len(payload['actionItems']), 'includedByDefault': sum(item['assignee'] == 'ole' for item in payload['actionItems']), 'version': payload['version'], 'payloadHash': fingerprint, 'createsTasks': False, 'plansDay': False}))
    if not args.write and not args.readback:
        return
    token = credential(args.credential_source)
    base = os.environ.get('MONOLITH_API_URL', DEFAULT_API).rstrip('/')
    source = '/agent/planning/meeting-proposals/by-source?' + urllib.parse.urlencode({'meetingId': payload['meetingId'], 'version': payload['version']})
    directory = Path(args.receipt_dir); directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / ('monolith-' + hashlib.sha256(f"{payload['meetingId']}:{payload['version']}".encode()).hexdigest() + '.md')
    previous = receipt.read_text() if receipt.exists() else ''
    if previous and f'Payload-Hash: {fingerprint}' not in previous:
        raise RuntimeError('Quelle geändert: gleiche Version nicht überschreiben; ausdrücklich neue Version nötig')
    job = request(base, token, source)
    if job is None and (args.readback or 'Write versucht' in previous):
        raise RuntimeError('Unsicherer Write ohne bestätigten Quelltreffer. Nicht wiederholen; Monolith-Phase bleibt offen.')
    if job is None:
        receipt.write_text(f'# Monolith-Meeting-Entwurf\n\nQuell-ID: {payload["meetingId"]}\nVersion: {payload["version"]}\nPayload-Hash: {fingerprint}\nRequest-ID: {payload["requestId"]}\nWrite versucht; Ergebnis offen.\nErfolgreiche externe Dokumentationsphasen bleiben erhalten.\n')
        try:
            request(base, token, '/agent/planning/meeting-proposals', payload)
        except Exception:
            # Only source-identity readback; never a second POST or other surface.
            job = request(base, token, source)
            if job is None:
                raise RuntimeError('Monolith-Write unklar; keine Wiederholung. Lokales Protokoll bleibt offen.') from None
        job = request(base, token, source)
    if not job or job.get('source', {}).get('meetingId') != payload['meetingId'] or job.get('source', {}).get('version') != payload['version']:
        raise RuntimeError('Quellidentität beim Rücklesen nicht bestätigt')
    metadata = job['source']
    if metadata.get('payloadHash') != fingerprint:
        raise RuntimeError('Kanonischer Inhalt beim Rücklesen abweichend; keine Wiederholung')
    for key in ('transcriptHash', 'actionItems', 'notionUrl', 'title', 'occurredAt'):
        if metadata.get(key) != payload.get(key):
            raise RuntimeError(f'Rücklesen stimmt nicht überein: {key}; keine Wiederholung')
    receipt.write_text(f'# Monolith-Meeting-Entwurf\n\nQuell-ID: {payload["meetingId"]}\nVersion: {payload["version"]}\nPayload-Hash: {fingerprint}\nMonolith-Job: {job["id"]}\nStatus: Entwurf rückgelesen; menschliche Prüfung offen.\nErfolgreiche externe Dokumentationsphasen bleiben erhalten.\nKeine Aufgaben oder Kalenderblöcke freigegeben.\n')
    print(f'Monolith-Entwurf rückgelesen. Lokales Protokoll: {receipt}')

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never expose response bodies, headers, credentials or extracted text.
        if isinstance(error, urllib.error.HTTPError):
            print(f'Monolith HTTP {error.code}; Phase offen, keine Wiederholung.', file=sys.stderr)
        elif isinstance(error, (ValueError, RuntimeError)):
            print(str(error), file=sys.stderr)
        else:
            print('Monolith-Verbindung/Rücklesen fehlgeschlagen; Phase offen, keine Wiederholung.', file=sys.stderr)
        sys.exit(1)
