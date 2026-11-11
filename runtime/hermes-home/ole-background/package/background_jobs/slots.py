"""Two host-wide process locks. Foreground work has priority before a new item."""
import fcntl
import os
import time
from pathlib import Path
from .contracts import private_dir

class Slot:
    def __init__(self, root, fd, index): self.root,self.fd,self.index=root,fd,index
    def close(self):
        if self.fd is not None: os.close(self.fd); self.fd=None
    def __enter__(self): return self
    def __exit__(self,*_): self.close()

def try_slot(root, *, background=False):
    root = private_dir(root)
    if background:
        if foreground_active(root):return None
        busy = root/'foreground-until'
        try:
            if float(busy.read_text()) > time.time(): return None
        except (OSError,ValueError): pass
    for index in range(2):
        p = root/('slot-'+str(index)+'.lock')
        fd = os.open(p,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: os.close(fd); continue
        return Slot(root,fd,index)
    return None

def foreground(root, seconds=30):
    root=private_dir(root)
    # Atomic replacement and monotonic extension, metadata only.
    import uuid
    lock=os.open(root/'foreground.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        target=root/'foreground-until'
        try: prior=float(target.read_text())
        except (OSError,ValueError): prior=0
        tmp=root/('foreground-'+uuid.uuid4().hex)
        tmp.write_text(str(max(prior,time.time()+seconds)));tmp.chmod(0o600);os.replace(tmp,target)
    finally: os.close(lock)

def session_foreground(root, owner, session_id, *, seconds=30, finished=False):
    from .contracts import digest
    root=private_dir(root)
    key=digest([owner,session_id]);folder=private_dir(root/'foreground-leases')
    lock=os.open(root/'foreground.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        target=folder/(key+'.lease')
        if target.is_symlink():raise ValueError('foreground_lease_symlink')
        if finished:target.unlink(missing_ok=True)
        else:
            if type(seconds) not in (int,float) or not 1<=seconds<=60:raise ValueError('bounded_foreground_ttl')
            import uuid
            tmp=folder/(key+'.'+uuid.uuid4().hex+'.tmp')
            tmp.write_text(str(time.time()+seconds));tmp.chmod(0o600);os.replace(tmp,target)
        return {'scope_hash':key,'active':not finished,'ttl_seconds':seconds if not finished else 0}
    finally:os.close(lock)

def foreground_active(root):
    folder=Path(root)/'foreground-leases'
    if not folder.exists():return False
    if folder.is_symlink():raise ValueError('foreground_lease_symlink')
    clock=time.time();active=False
    for p in folder.glob('*.lease'):
        if p.is_symlink():raise ValueError('foreground_lease_symlink')
        try:
            if float(p.read_text())>clock:active=True
            else:p.unlink(missing_ok=True)
        except (OSError,ValueError):continue
    return active

def session_is_foreground(root,owner,session_id):
    from .contracts import digest
    target=Path(root)/'foreground-leases'/(digest([owner,session_id])+'.lease')
    if target.is_symlink() or target.parent.is_symlink():raise ValueError('foreground_lease_symlink')
    try:return float(target.read_text())>time.time()
    except FileNotFoundError:return False
