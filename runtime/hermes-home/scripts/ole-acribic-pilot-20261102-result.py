"""One factual end-of-pilot notice; no model and no email writes."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import subprocess
import sys

ROOT = Path('/home/hermes/hermes-acribic-pilot-20261102/final-1901')
PYTHON = '/home/hermes/.hermes/hermes-agent/venv/bin/python'
RUN = ROOT / 'runs/acribic-20261102-N10-0701-0719'


def format_notice(value):
    sent, crm = value['sent_confirmed'], value['crm_confirmed']
    if sent == crm == 10 and not value['send_uncertain'] and not value['crm_open']:
        text = 'Die zehn Acribic-Mails sind versendet und im CRM eingetragen.'
    else:
        text = f'Beim Acribic-Test sind {sent} von zehn Mails versendet und {crm} im CRM eingetragen.'
        if value['send_uncertain']:
            text += ' Ein Versandversuch ist noch unklar; der Lauf ist gestoppt.'
        elif value['crm_open']:
            text += ' Der CRM-Abschluss ist noch offen; der Lauf ist gestoppt.'
        elif value['not_attempted']:
            text += ' Die übrigen Mails wurden nicht versendet; der Lauf ist gestoppt.'
    if value.get('bounces'):
        text += f" {value['bounces']} Mail kam bisher als unzustellbar zurück."
    elif value.get('delivery_uncertain'):
        text += ' Eine Zustellmeldung braucht noch eine Prüfung.'
    return text


def main():
    result = subprocess.run([PYTHON, '-B', str(ROOT / 'candidate/launch.py'), '--root', str(ROOT), 'result'],
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        # The scheduler records the actual failure. Never invent a delivery count.
        print('pilot_result_read_failed', file=sys.stderr)
        return 2
    value = json.loads((RUN / 'result.json').read_text())
    assert datetime.fromisoformat(value['at']).date().isoformat() == '2026-11-02'
    assert datetime.now(ZoneInfo('Europe/Berlin')).hour >= 19
    sys.path.insert(0, str(ROOT / 'candidate'))
    from acribic_pilot.central import Central, atomic
    central = Central(ROOT / 'central')
    with central.lock():
        activation = json.loads((ROOT / 'activation.json').read_text())
        atomic(ROOT / 'activation.json', {**activation, 'enabled': False})
    print(format_notice(value))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
