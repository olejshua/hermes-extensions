"""Fail-closed filesystem primitives; private state is never model writable."""
import hashlib
import json
import os
import stat
from pathlib import Path

class Fault(Exception):
    def __init__(self, code): self.code=code; super().__init__(code)

def checked(path, *, directory=False):
    p=Path(path)
    if not p.is_absolute() or '..' in p.parts: raise Fault('unsafe_path')
    for part in (p,*p.parents):
        if part.is_symlink(): raise Fault('symlink_forbidden')
    if p.exists():
        s=p.stat()
        if s.st_uid!=os.getuid(): raise Fault('foreign_owner')
        if directory and not stat.S_ISDIR(s.st_mode): raise Fault('directory_required')
    return p

def private(path):
    p=checked(path,directory=True)
    p.mkdir(mode=0o700,parents=True,exist_ok=True)
    if p.stat().st_mode&0o077: raise Fault('private_directory_required')
    return p

def read_file(path,limit=2_000_000):
    p=checked(path)
    # Walk every component with dirfd + O_NOFOLLOW, closing the check/open
    # race for model-writable intermediate artifact directories.
    parent=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in p.parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            os.close(parent);parent=child
        fd=os.open(p.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    finally:os.close(parent)
    try:
        s=os.fstat(fd)
        if s.st_uid!=os.getuid():raise Fault('foreign_owner')
        if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1: raise Fault('regular_single_link_file_required')
        if s.st_size>limit: raise Fault('file_too_large')
        with os.fdopen(fd,'rb',closefd=False) as f: b=f.read(limit+1)
        if len(b)>limit: raise Fault('file_too_large')
        return b
    finally: os.close(fd)

def write_private(path,data):
    p=checked(path); data=data.encode() if isinstance(data,str) else data
    tmp=p.with_name(p.name+'.new-'+os.urandom(8).hex())
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb',closefd=False) as f: f.write(data);f.flush();os.fsync(fd)
        os.replace(tmp,p)
        d=os.open(p.parent,os.O_RDONLY);os.fsync(d);os.close(d)
    finally:
        os.close(fd)
        tmp.unlink(missing_ok=True)

def digest(data):
    if not isinstance(data,bytes): data=json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()

def text(value,limit=16000):
    if not isinstance(value,str) or not value.strip() or len(value)>limit or '\0' in value: raise Fault('invalid_text')
    return value

def artifact_path(work,relative):
    if not isinstance(relative,str) or not relative.startswith('artifacts/') or len(relative)>240: raise Fault('unsafe_artifact')
    parts=Path(relative).parts
    if any(x in ('.','..') or x.startswith('.') for x in parts) or Path(relative).is_absolute():raise Fault('unsafe_artifact')
    p=checked(Path(work)/relative)
    if not p.is_relative_to(Path(work)/'artifacts'): raise Fault('unsafe_artifact')
    if p.suffix.lower() not in ('.md','.txt','.csv','.html','.png','.jpg','.jpeg','.webp','.svg'): raise Fault('artifact_type_forbidden')
    return p
