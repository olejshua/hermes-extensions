"""One native tool invokes the existing managed read-only calendar runtime."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

HOME = Path('/home/hermes/.hermes')
PYTHON = HOME/'hermes-agent/venv/bin/python3'
SCRIPT = HOME/'scripts/email_realtime.py'


def run(args):
    starts = args.get('starts')
    if (set(args) != {'starts'} or not isinstance(starts,list) or not 1 <= len(starts) <= 32
            or any(not isinstance(s,str) or not 1 <= len(s) <= 40 for s in starts)):
        return {'status':'unknown','error':'exact_starts_required','reserved':False}
    try:
        result = subprocess.run([str(PYTHON),str(SCRIPT),'availability',
                                 '--starts-json',json.dumps(starts)],
                                capture_output=True,text=True,timeout=60)
        if result.returncode or len(result.stdout.encode()) > 65536:
            raise ValueError('calendar_runtime_unavailable')
        value = json.loads(result.stdout)
        if (value.get('contract') != 'ole-calendar-check-v1' or value.get('reserved') is not False
                or value.get('hold_minutes') != 30
                or value.get('status') not in ('checked','unknown')
                or not isinstance(value.get('slots'),list) or len(value['slots']) != len(starts)
                or any(row.get('state') not in ('free','busy','unknown') for row in value['slots'])):
            raise ValueError('calendar_runtime_unverified')
        for source,row in zip(starts,value['slots']):
            original=datetime.fromisoformat(source.replace('Z','+00:00'))
            returned=datetime.fromisoformat(row['start'].replace('Z','+00:00'))
            if original.tzinfo is None or returned.tzinfo is None or original != returned:
                raise ValueError('calendar_start_changed')
        if value['status']=='checked':
            checked=datetime.fromisoformat(value['checked_at'])
            if (checked.tzinfo is None or not timedelta(0)<=datetime.now(timezone.utc)-checked<=timedelta(seconds=60)
                    or any(row['state']=='unknown' for row in value['slots'])):
                raise ValueError('calendar_evidence_unverified')
        return value
    except Exception:
        return {'status':'unknown','error':'calendar_runtime_unavailable','reserved':False}
