import argparse
import json
import signal
import time
from .safety import Fault,read_file
from .service import ProjectService
from .worker import Supervisor
from .preflight import preflight

def main():
    p=argparse.ArgumentParser();p.add_argument('--policy',required=True);p.add_argument('action',choices=['health','preflight','work-one','supervise','reconcile']);p.add_argument('--seconds',type=int,default=300);p.add_argument('--job-id');a=p.parse_args()
    try:
        policy=json.loads(read_file(a.policy,1_000_000));s=ProjectService(policy)
        if a.action=='health':out={'ok':True,'service':'ole_project_work','storage':True,'installation_activated':False}
        elif a.action=='preflight':out=preflight(policy)
        elif a.action=='reconcile':out=Supervisor(s).recover_uncertain(a.job_id)
        elif a.action=='work-one':out=Supervisor(s).run_one()
        else:
            if not 1<=a.seconds<=3600:raise Fault('bounded_supervisor_seconds_required')
            def stop(*_):raise KeyboardInterrupt
            signal.signal(signal.SIGTERM,stop);deadline=time.monotonic()+a.seconds
            while time.monotonic()<deadline:
                out=Supervisor(s).run_one()
                if out.get('state') in ('idle','supervisor_busy','waiting_for_native_slot'):time.sleep(min(2,max(0,deadline-time.monotonic())))
            out={'ok':True,'state':'supervisor_window_ended'}
        print(json.dumps(out,ensure_ascii=False));return 0 if out.get('ok') else 1
    except Fault as e:print(json.dumps({'ok':False,'error':e.code}));return 1
    except KeyboardInterrupt:return 130
if __name__=='__main__':raise SystemExit(main())
