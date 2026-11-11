"""Exact process ownership; a worker group dies when its supervisor dies."""
import os
from pathlib import Path
import signal
import time
import subprocess

def birth(pid):
    try:
        # /proc starttime is monotonic kernel identity, immune to PID reuse.
        stat=Path('/proc')/str(pid)/'stat'
        if stat.exists():return stat.read_text().rsplit(')',1)[1].split()[19]
        if os.uname().sysname=='Linux':return None
        value=subprocess.check_output(['ps','-p',str(pid),'-o','lstart='],text=True).strip()
        return value or None
    except (OSError,ValueError,subprocess.SubprocessError):return None

def alive(pid,expected_birth):
    if birth(pid)!=expected_birth:return False
    try:
        stat=Path('/proc')/str(pid)/'stat'
        if stat.exists():return stat.read_text().rsplit(')',1)[1].split()[0] not in {'Z','X'}
        state=subprocess.check_output(['ps','-p',str(pid),'-o','stat='],text=True).strip()
        return bool(state) and not state.startswith('Z')
    except (OSError,subprocess.SubprocessError):return False

def parent_death():
    if os.uname().sysname=='Linux':
        import ctypes
        libc=ctypes.CDLL(None)
        if libc.prctl(1,signal.SIGTERM,0,0,0)!=0:raise OSError('parent_death_signal_unavailable')
        if os.getppid()==1:os._exit(76)

def group_stop(pid, expected_birth):
    if not alive(pid,expected_birth):return False
    try:
        if os.getpgid(pid)!=pid:return False
        os.killpg(pid,signal.SIGTERM)
    except ProcessLookupError:return False
    until=time.monotonic()+3
    while time.monotonic()<until and alive(pid,expected_birth):time.sleep(.05)
    if alive(pid,expected_birth):
        try:os.killpg(pid,signal.SIGKILL)
        except ProcessLookupError:pass
    return True
