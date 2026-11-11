"""Reviewable derivative-only cleanup; never deletes canonical sources."""
from pathlib import Path
from .manifest import load_manifest
from .state import State
from .ov_adapter import OpenVikingAdapter, is_safe_resource_uri
import json

def cleanup(manifest: Path, apply=False):
 m=load_manifest(manifest);state=State(m.state_db);adapter=OpenVikingAdapter(binary=m.ov_binary)
 with state.connect() as c: rows=c.execute("SELECT source_id,ov_resource_id FROM resources WHERE status='deleted' AND ov_resource_id IS NOT NULL").fetchall()
 targets=[]
 for r in rows:
  uri=r['ov_resource_id']
  if not is_safe_resource_uri(uri) or not (uri.startswith('viking://resources/ole-approved/') or uri.startswith('viking://resources/ole_unconfirmed/context/')):raise ValueError('unapproved cleanup namespace')
  targets.append(uri)
  if apply:
   adapter.remove_resource(uri,recursive=True)
   with state.connect() as c:c.execute("UPDATE resources SET ov_resource_id=NULL WHERE source_id=? AND status='deleted'",(r['source_id'],))
 return {'apply':apply,'derived_index_targets':targets,'canonical_sources_changed':False}
if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--apply',action='store_true');a=p.parse_args();print(json.dumps(cleanup(a.manifest,a.apply)))
