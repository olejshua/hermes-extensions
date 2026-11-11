"""Trusted fresh native empty-tool briefing probe; never delivers a message."""
import argparse
import datetime as dt
import json
import sys
import re
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--input',required=True)
    p.add_argument('--state',required=True);p.add_argument('--baseline',action='store_true');args=p.parse_args()
    sys.path.insert(0,args.source)
    from gtm import briefing_daemon as b
    snapshot=json.loads(Path(args.input).read_text());context=snapshot.get('context');raw=snapshot.get('model_output')
    if not context or not isinstance(raw,str):raise SystemExit('complete_original_model_case_required')
    starts=dt.datetime.fromisoformat(context['calendar']['start']['dateTime'])
    if args.baseline:
        try:b.validate_output(raw,context,starts_at=starts)
        except Exception:print(json.dumps({'baseline_failed':True}));return
        raise SystemExit('baseline_did_not_reproduce_failure')
    context['briefing_job_id']='a'+snapshot['identity'][1:]
    raw=b.Runtime(b.Store(args.state),target=b.DESTINATION).model(context)
    result=b.validate_output(raw,context,starts_at=starts)
    print(json.dumps({'candidate_passed':True,'native_briefing':result},ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except (Exception,SystemExit) as error:
        if isinstance(error,SystemExit) and error.code in (None,0):raise
        value=str(getattr(error,'code',None) or error)
        code=value if re.fullmatch(r'[A-Za-z0-9_]{1,100}',value) else type(error).__name__
        # No SDK stderr/auth headers, source body or raw reasoning in feedback.
        print(json.dumps({'probe_failed':True,'error_type':type(error).__name__,'validation_code':code}))
        raise SystemExit(1)
