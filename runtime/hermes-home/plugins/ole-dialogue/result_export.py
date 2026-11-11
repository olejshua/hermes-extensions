"""Complete private readonly results; no provider calls or content approval."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import stat

MAX_BYTES = 16_000_000


def export_result(root, backend, row, owner, session):
    if row.get('owner') != owner or row.get('session_id') != session or row.get('profile') != 'readonly-research':
        raise ValueError('result_route_mismatch')
    job=row['job_id'];family=row.get('family_members',1)>1
    total=row.get('family_total') if family else row.get('total')
    if type(total) is not int or not 1 <= total <= 500:
        raise ValueError('result_count_invalid')
    parts=['# Hintergrundauftrag\n\nErgebnisse zur Prüfung. Keine Inhaltsfreigabe.\n']
    seen=set(); offset=0; size=len(parts[0].encode())
    def append(text):
        nonlocal size
        size+=len(text.encode('utf-8'))
        if size>MAX_BYTES:raise ValueError('result_attachment_too_large')
        parts.append(text)
    while True:
        page=(backend.family_page if family else backend.item_page)(job,owner,session,offset=offset,length=100)
        items=page.get('items')
        if (page.get('job_id')!=job or page.get('total')!=total or page.get('truncated') is not False
                or not isinstance(items,list) or not 1<=len(items)<=100):
            raise ValueError('result_item_page_invalid')
        for item in items:
            item_id=item.get('item_id')
            if not isinstance(item_id,str) or not item_id or item_id in seen:
                raise ValueError('result_item_identity_invalid')
            seen.add(item_id)
            append('\n## '+item_id+'\n\n')
            origin=item.get('origin_job_id') if family else job
            scope=item.get('origin_scope_hash') if family else row.get('scope_hash')
            if not isinstance(origin,str) or not origin or not isinstance(scope,str) or not scope:raise ValueError('result_origin_invalid')
            if family:append('Ursprünglicher Auftrag: '+origin+'\nScope SHA256: '+scope+'\n\n')
            if item.get('state')!='verified':
                if item.get('state')!='blocked':raise ValueError('result_item_not_terminal')
                append('Blockiert: '+str(item.get('blocked_reason') or 'Grund nicht belegt')+'\n')
                continue
            pos=0; identity=None; hasher=hashlib.sha256()
            while True:
                result=backend.result_read(origin,item_id,owner,session,offset=pos,length=40000)
                text=result.get('text'); end=pos+len(text) if isinstance(text,str) else -1
                keys=('job_id','item_id','source_hash','source_version','scope_hash','analysis_sha256','total_characters')
                current=tuple(result.get(k) for k in keys)
                if (result.get('job_id')!=origin or result.get('item_id')!=item_id
                        or result.get('scope_hash')!=scope
                        or result.get('offset')!=pos or result.get('offset_unit')!='unicode_codepoints'
                        or not isinstance(text,str) or len(text)>40000
                        or type(result.get('total_characters')) is not int or result['total_characters']<end
                        or result.get('truncated') is not False or result.get('result_review_required') is not True
                        or result.get('content_approval') is not None or result.get('ready_for_send') is not False
                        or result.get('no_claim_approval') is not True
                        or type(result.get('source_version')) is not int or result['source_version'] < 1
                        or not all(isinstance(result.get(k),str) and result[k] for k in ('source_hash','analysis_sha256'))
                        or (identity is not None and current!=identity)):
                    raise ValueError('result_page_invalid')
                identity=current
                append(text);hasher.update(text.encode('utf-8'))
                next_offset=result.get('next_offset')
                if next_offset is None:
                    if end!=result['total_characters'] or hasher.hexdigest()!=result['analysis_sha256']:
                        raise ValueError('result_hash_or_coverage_mismatch')
                    break
                if type(next_offset) is not int or next_offset!=end or end<=pos or end>=result['total_characters']:
                    raise ValueError('result_cursor_invalid')
                pos=end
            append('\n\nSource SHA256: '+identity[2]+'\nAnalysis SHA256: '+identity[5]+'\n')
        offset+=len(items)
        if page.get('next_offset') is None:
            if offset!=total:raise ValueError('result_item_coverage_missing')
            break
        if type(page['next_offset']) is not int or page['next_offset']!=offset or offset>=total:
            raise ValueError('result_item_cursor_invalid')
    root=Path(root)
    if any(p.is_symlink() for p in (root,*root.parents)):
        raise ValueError('unsafe_result_root')
    st=root.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid():raise ValueError('unsafe_result_root')
    folder=root/'result-exports'
    try:folder.mkdir(mode=0o700)
    except FileExistsError:pass
    st=folder.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.getuid():raise ValueError('unsafe_result_directory')
    os.chmod(folder,0o700)
    body=''.join(parts).encode('utf-8')
    path=folder/(hashlib.sha256(job.encode()).hexdigest()+'.md')
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            st=os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o600:
                raise ValueError('unsafe_result_file')
            if stream.read(MAX_BYTES+1)!=body:raise ValueError('result_export_conflict')
    else:
        with os.fdopen(fd,'wb') as stream:
            stream.write(body);stream.flush();os.fsync(stream.fileno())
    return path
