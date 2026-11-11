#!/usr/bin/env python3
"""One Dream-only index attempt using the existing registry, leases and adapter.

No approved/unconfirmed backfill, mail import or empty heading publication.
Run by Root's reviewed night worker, never by the candidate author in production.
"""
import argparse
import hashlib
import sqlite3
import time
import uuid
from dataclasses import replace
from pathlib import Path

from hermes_second_brain.dreaming.config import load_dreaming_config
from hermes_second_brain.manifest import load_manifest
from hermes_second_brain.scanner import scan_source
from hermes_second_brain.state import State
from hermes_second_brain.ov_adapter import OpenVikingAdapter
from hermes_second_brain.sync import _sync_one, openviking_lease_seconds


def run(config_path: Path, *, budget_seconds: float = 120) -> dict:
    deadline=time.monotonic()+max(.001,min(120,float(budget_seconds)))
    config=load_dreaming_config(config_path)
    if not config.enabled:
        return {'state':'disabled','attempts':0}
    manifest=load_manifest(config_path)
    source=next((s for s in manifest.sources if s.id=='dream-conclusions' and s.namespace=='dreams' and s.root==config.import_dir),None)
    if source is None:
        raise ValueError('exact_dream_source_required')
    if not config.dream_state_db.is_file():
        return {'state':'quiet','attempts':0}
    with sqlite3.connect(config.dream_state_db.resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        if 'indexable' not in {row[1] for row in db.execute('PRAGMA table_info(publications)')}:
            return {'state':'quiet_no_indexable_material','attempts':0}
        rows=db.execute("""SELECT p.* FROM publications p WHERE p.indexable=1
          AND p.status='locally_published'
          AND EXISTS(SELECT 1 FROM runs r WHERE r.run_id=p.run_id AND r.status='complete')
          ORDER BY p.created_at DESC LIMIT 200""").fetchall()
    paths=[]
    for row in rows:
        path=Path(row['final_import_path'])
        if path.parent!=config.import_dir or path.is_symlink() or not path.is_file():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['content_hash']:
            raise ValueError('dream_publication_hash_changed')
        paths.append(path.name)
    if not paths:
        return {'state':'quiet_no_indexable_material','attempts':0}
    registry=State(manifest.state_db)
    registry.upsert_scan(scan_source(replace(source,approved_paths=tuple(paths))),source.id)
    owner='dream-sync-'+uuid.uuid4().hex
    batch=registry.claim_pending(limit=1,lease_seconds=openviking_lease_seconds(manifest),owner=owner,
                                 include_failed=True,allowed_source_root_ids={source.id})
    if not batch:
        return {'state':'quiet','attempts':0}
    row=batch[0]
    class BoundedAdapter(OpenVikingAdapter):
        def _run(self,command):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('dream_sync_deadline')
            self.timeout_seconds=min(30,remaining)
            return super()._run(command)
    adapter=BoundedAdapter(binary=manifest.ov_binary,timeout_seconds=max(.001,min(30,manifest.ov_timeout_seconds,deadline-time.monotonic())),attempts=1,backoff_seconds=0)
    try:
        remote=_sync_one(adapter,row,resume_locked=False)
        adapter.wait()
        if not registry.mark_synced(row.source_id,row.sha256,remote,owner=owner):
            raise RuntimeError('stale_dream_sync_receipt')
        return {'state':'synced','attempts':1,'source_id':row.source_id,'content_sha256':row.sha256,'remote_id':remote}
    except Exception as exc:
        registry.mark_failed(row.source_id,row.sha256,type(exc).__name__,owner=owner)
        return {'state':'index_pending','attempts':1}


def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True);parser.add_argument('--budget-seconds',type=float,default=120);args=parser.parse_args(argv)
    result=run(args.config,budget_seconds=args.budget_seconds)
    return 0 if result['state'] in {'disabled','quiet','quiet_no_indexable_material','synced'} else 1


if __name__=='__main__':raise SystemExit(main())
