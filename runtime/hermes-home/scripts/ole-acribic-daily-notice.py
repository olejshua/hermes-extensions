from pathlib import Path
import subprocess,sys
r=subprocess.run(['/home/hermes/.hermes/hermes-agent/venv/bin/python','-B','/home/hermes/hermes-acribic-daily-20261102/daily.py','notice'],capture_output=True,text=True,timeout=180)
if r.returncode:
    print('daily_result_read_failed',file=sys.stderr)
    raise SystemExit(2)
print(r.stdout.strip())
