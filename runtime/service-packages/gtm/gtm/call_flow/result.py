"""Persist the current confirmed target state; keep the first result as history."""
import json
import os
from pathlib import Path
import tempfile


def save(directory, result):
    directory=Path(directory)
    data=json.dumps(result,sort_keys=True,ensure_ascii=False).encode()
    path=directory/'finalize-result.json'
    if path.is_symlink() or directory.is_symlink():raise ValueError('symlink_call_result')
    if path.exists():
        before=path.read_bytes()
        if before==data:return
        initial=directory/'finalize-result.initial.json'
        try:fd=os.open(initial,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:
            if initial.is_symlink():raise ValueError('symlink_call_result')
        else:
            with os.fdopen(fd,'wb')as stream:stream.write(before);stream.flush();os.fsync(stream.fileno())
    fd,tmp=tempfile.mkstemp(prefix='.finalize-result-',dir=directory)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb')as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
        parent=os.open(directory,os.O_RDONLY)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
