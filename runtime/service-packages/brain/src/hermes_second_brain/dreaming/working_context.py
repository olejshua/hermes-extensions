"""Current, source-bound work and lossless, budgeted views of large originals.

The configured receipt is an explicit read grant, not a memory/rule promotion.
Runtime jobs are read only; leases, processes, credentials and claims are never
copied. View omissions are explicit and full originals stay in the run material.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SOURCE = "current_work_context"


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class ContextSources(list):
    def __init__(self, values=(), originals=None):
        super().__init__(values)
        self.originals = originals or {}


def sections(text, size):
    """Exact contiguous slices; concatenation always reconstructs the original."""
    offset=0
    while offset<len(text):
        stop=min(len(text),offset+size)
        if stop<len(text):
            boundary=text.rfind("\n",offset+size//2,stop)
            if boundary>=0:stop=boundary+1
        yield offset,stop,text[offset:stop]
        offset=stop


def terms(text):
    return {w for w in re.findall(r"[\w-]{4,}",text.casefold())}


def view(original, budget, focus=""):
    """A derived view always fits; full originals and explicit omissions remain."""
    if len(encoded(original))<=budget:return original
    interest=terms(focus);material=[];index=[]
    project=original["project"];brief={};omitted_fields={}
    for key,value in project.items():
        if len(encoded(value))<=(min(1024,budget//6) if key=="description" else 256) and key not in {"notes","content"}:brief[key]=value
        else:
            omitted_fields[key]={"characters":len(encoded(value)),"sha256":digest(value),"original_field":f"project.{key}"}
            if key=="name":brief[key]=str(value)[:200];brief["name_complete"]=False
    coverage=original["coverage"]
    base={"version":"dream-project-view/v1","providerVersion":original["version"],
        "providerSnapshotSha256":digest({k:v for k,v in original.items() if k!="capturedAt"}),
        "capturedAt":original["capturedAt"],"focusTaskId":original.get("focusTaskId"),"project":brief,
        "project_omitted_fields":omitted_fields,"requiredScopes":original["requiredScopes"],
        "coverage":{k:coverage.get(k) for k in ("complete","counts","limits","missingScopes")},
        "sources":[x for x in original["sources"] if x.get("type")=="project"],"links":[],
        "links_in_original":True,"source_catalog_in_original":True}
    unavailable=coverage.get("unavailable",[])
    base["coverage"]["unavailable"]=[{"source_ref":str(item.get("id","unknown"))[:120]} if isinstance(item,dict) else {"original_index":i} for i,item in enumerate(unavailable[:10])]
    base["coverage"]["unavailable_original_count"]=len(unavailable)
    versions={str(x.get("id")):x.get("version") for x in original["sources"]}
    for group in ("tasks","documents","decisions","assignments","planningGoals","openItems"):
        rows=original.get(group);base[group]=None if rows is None else []
        for i,row in enumerate(rows or []):
            identity=str(row.get("id") or (row.get("assignment") or {}).get("id") or i)
            title=str(row.get("title") or row.get("name") or (row.get("assignment") or {}).get("title") or identity)
            state={key:row[key] for key in ("status","completed","resolved_at","updated_at","created_at") if key in row and len(encoded(row[key]))<150}
            ref=f"{group}:{identity}";text=encoded(row)
            index.append({"ref":ref,"title":title[:200],"title_complete":len(title)<=200,"title_characters":len(title),
                "state":state,"source_version":versions.get(identity),"row_sha256":digest(row),"characters":len(text)})
            material.append((len(terms(text)&interest),group,ref,row))
    pages=[];page=[]
    for entry in index:
        if page and len(encoded([*page,entry]))>budget//3:pages.append(page);page=[]
        page.append(entry)
    if page:pages.append(page)
    base["selectionCoverage"]={"complete":False,"providerComplete":coverage["complete"],"selected":[],
        "index_pages":len(pages),"index_page":0,"index":pages[0] if pages else [],"total_entities":len(index),
        "full_original_available":True,"large_titles_are_explicit_previews":True}
    for _,group,ref,row in sorted(material,key=lambda x:-x[0]):
        trial=json.loads(encoded(base));trial[group].append(row);trial["selectionCoverage"]["selected"].append(ref)
        if len(encoded(trial))<=budget-300:base=trial
    base["selectionCoverage"]["omitted_count"]=len(index)-len(base["selectionCoverage"]["selected"])
    if len(encoded(base))>budget:
        # Metadata pages are addressable too, never silently called complete.
        base["selectionCoverage"]["index"]=[];base["selectionCoverage"]["index_page_not_loaded"]=True
    if len(encoded(base))>budget:raise ValueError("provider_control_metadata_exceeds_view_budget")
    return base


def source_parts(entry, budget):
    from .redaction import redact
    text=redact(entry["original"])
    for start,stop,part in sections(text,max(400,budget-1200)):
        yield {**{k:v for k,v in entry.items() if k!="original"},"original":part,
               "coverage":{"complete":start==0 and stop==len(text),"start":start,"end":stop,
                           "total_characters":len(text),"original_sha256":entry["source_version"],"offset_basis":"redacted_original"}}


def load(config, now):
    from .ingest import MessageCapsule,SessionCapsule
    from .redaction import redact
    entries=[];issues=[];runtime_coverage={"available":config.project_results_db is None,"selected":0,"total":None,"complete":config.project_results_db is None}
    path=config.working_context
    if path is not None:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size>2*1024*1024:
                raise ValueError("work_receipt_unavailable")
            packet=json.loads(path.read_text())
            if packet.get("version")!="dream-working-context/v1" or not isinstance(packet.get("entries"),list):
                raise ValueError("work_receipt_contract_invalid")
            captured=datetime.fromisoformat(packet["captured_at"].replace("Z","+00:00"))
            if captured.tzinfo is None:raise ValueError("work_receipt_capture_invalid")
            stale=(now-captured).total_seconds()>48*3600 or (now-captured).total_seconds() < -300
            if stale:issues.append({"reason":"scoped_work_snapshot_stale_dynamic_sources_remain_available"})
            for original_row in packet["entries"]:
                try:
                    row={**original_row,"receipt_captured_at":packet["captured_at"],"snapshot_stale":stale}
                    stamp=datetime.fromisoformat(row["source_date"].replace("Z","+00:00"))
                    if stamp.tzinfo is None:raise ValueError("source_date_timezone_missing")
                    if hashlib.sha256(row["original"].encode()).hexdigest()!=row["source_version"]:
                        raise ValueError("work_source_version_invalid")
                    if row.get("role") not in {"user","assistant"} or not isinstance(row.get("source_uri"),str):
                        raise ValueError("work_source_authority_invalid")
                    entries.append(row)
                except (ValueError,TypeError,KeyError,AttributeError):
                    issues.append({"reason":"current_work_entry_invalid_excluded"})
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            issues.append({"reason":"current_work_receipt_unavailable_or_invalid"})
    if config.project_results_db is not None:
        path=config.project_results_db
        try:
            with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as db:
                total=db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                valid=db.execute("SELECT COUNT(*) FROM jobs WHERE json_valid(doc)").fetchone()[0]
                projection=",".join("'"+key+"',json_extract(doc,'$."+key+"')" for key in
                    ("id","project","goal","instructions","state","revision","summary","uncertainties","artifacts","created_at","updated_at"))
                rows=db.execute("SELECT id,json_object("+projection+") FROM jobs WHERE json_valid(doc) ORDER BY CAST(json_extract(doc,'$.updated_at') AS REAL) DESC,id LIMIT 30").fetchall()
                runtime_coverage={"available":True,"selected":len(rows),"total":total,"complete":len(rows)==total,
                    "order":"updated_at_desc_id","limit":30,"invalid_rows":total-valid,"omitted_older_rows":max(0,valid-len(rows))}
                if valid<total:issues.append({"reason":"project_results_invalid_rows_excluded","count":total-valid})
                if len(rows)<valid:issues.append({"reason":"project_results_bounded_recent_view","selected":len(rows),"total":total,"order":"updated_at_desc_id"})
                for database_id,raw in rows:
                    try:
                        job=json.loads(raw)
                        if not isinstance(job.get("id"),str) or job["id"]!=database_id:raise ValueError("job_identity_mismatch")
                        stamp=datetime.fromtimestamp(job["updated_at"],timezone.utc)
                        original={k:job.get(k) for k in ("id","project","goal","instructions","state","revision","summary","uncertainties","artifacts")}
                        entries.append({"id":job["id"],"kind":"execution_receipt","role":"assistant",
                            "source_uri":"hermes-project-job:"+job["id"],"source_date":stamp.isoformat(),
                            "source_version":hashlib.sha256(encoded(original).encode()).hexdigest(),
                            "original":encoded(original),"work_state":job["state"],"authority":"execution_record_not_new_human_instruction"})
                    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
                        issues.append({"reason":"project_result_entry_invalid_excluded"})
                        runtime_coverage["selected"]-=1
                        runtime_coverage["invalid_entries"]=runtime_coverage.get("invalid_entries",0)+1
                        runtime_coverage["complete"]=False
        except (OSError,sqlite3.Error):issues.append({"reason":"project_results_unavailable"})
    result=ContextSources(originals={e["source_uri"]:e for e in entries})
    result.issues=issues
    result.runtime_coverage=runtime_coverage
    result.entries_by_ref={}
    result.trigger_keys=set()
    grouped={}
    for entry in entries:
        stamp=datetime.fromisoformat(entry["source_date"].replace("Z","+00:00"))
        for i,part in enumerate(source_parts(entry,config.budgets.characters_per_session)):
            ref="work:"+hashlib.sha256((entry["source_uri"]+f"#{i}").encode()).hexdigest()[:32]
            clean=redact(part["original"])
            message=MessageCapsule(ref,entry["role"],clean,stamp.timestamp(),entry["source_version"],clean!=part["original"],entry["role"]=="user")
            result.entries_by_ref[ref]={k:v for k,v in part.items() if k!="original"}
            group=("scoped-codex:"+entry["kind"]) if entry["source_uri"].startswith("codex-thread:") else ref
            grouped.setdefault(group,[]).append((message,part,stamp))
    for group,parts in grouped.items():
        # Preserve all original messages. Existing batching counts the whole
        # bounded work capsule, with per-source coverage when text is split.
        chunks=[];current=[];characters=0
        for part in parts:
            if current and characters+len(part[0].text)>config.budgets.characters_per_session:
                chunks.append(current);current=[];characters=0
            current.append(part);characters+=len(part[0].text)
        if current:chunks.append(current)
        for index,chunk in enumerate(chunks):
            messages=tuple(p[0] for p in chunk);stamp=max(p[2].timestamp() for p in chunk)
            trigger=any((m.role=="user" and not meta.get("snapshot_stale",False)) or
                (meta.get("kind") in {"execution_receipt","delegated_task"} and not meta.get("snapshot_stale",False) and 0<=(now-t).total_seconds()<=14*86400) for m,meta,t in chunk)
            fingerprint=digest([{k:v for k,v in part.items() if k not in {"receipt_captured_at","snapshot_stale","source_date"}} for _,part,_ in chunk])
            key="current-work:"+group+f":part:{index}"
            if trigger:result.trigger_keys.add(key)
            result.append(SessionCapsule("current-work",group+f":part:{index}",SOURCE,"Current work, original receipts",stamp,stamp,
                messages,(),fingerprint,any(m.role=="user" and not part.get("snapshot_stale",False) for m,part,_ in chunk)))
    return result,issues


def packet(sources, sessions):
    entries=getattr(sources,"entries_by_ref",{})
    selected={m.ref:{**entries.get(m.ref,{}),"original":m.text} for s in sessions if s.source==SOURCE for m in s.messages}
    active=[]
    for uri,original in getattr(sources,"originals",{}).items():
        if original.get("kind") not in {"human_instruction","delegated_task","execution_receipt"}:continue
        entry={k:original.get(k) for k in ("id","source_uri","source_date","kind","title","work_state","authority","source_version","timestamp_basis","snapshot_stale")}
        entry["state_is_as_of_source_time"]=True
        if original.get("kind")=="execution_receipt":
            body=json.loads(original["original"]);entry.update(goal=body.get("goal"),reported_summary=body.get("summary"),artifact_bytes_loaded=False)
        active.append(entry)
    return {"selected_originals":selected,"other_work_index":active,"runtime_result_coverage":getattr(sources,"runtime_coverage",{}),"source_issues":getattr(sources,"issues",[]),
            "other_work_index_is_not_full_original":True,"full_originals_available_for_query":True}


def evidence(sources, sessions, config):
    from .store import EvidenceRecord,snippet_hash
    from .redaction import redact
    result={}
    for session in sessions:
        if session.source!=SOURCE:continue
        for message in session.messages:
            meta=getattr(sources,"entries_by_ref",{}).get(message.ref,{})
            snippet=redact(message.text)[:config.budgets.evidence_snippet_characters]
            provenance={"source_uri":meta.get("source_uri"),"source_version":message.content_hash,
                "source_date":meta.get("source_date"),"timestamp_basis":meta.get("timestamp_basis","source_message_time"),
                "authority":meta.get("authority"),"work_state":meta.get("work_state"),"coverage":meta.get("coverage"),
                "evidence_status":"historical" if meta.get("work_state")=="historical_report" or meta.get("snapshot_stale") else "confirmed" if message.role=="user" else "unknown"}
            day=datetime.fromtimestamp(message.timestamp,timezone.utc).date().isoformat()
            result[message.ref]=EvidenceRecord(message.ref,session.profile,session.session_id,message.role,day,snippet,snippet_hash(snippet),provenance)
    return result


def relevant(project, focus, now):
    """Old imported tasks alone are not evidence of a current human priority."""
    if project["project"]["name"].casefold() in focus.casefold():return True
    rows=[project["project"],*(project.get("tasks") or []),*(project.get("documents") or []),*(project.get("decisions") or [])]
    for row in rows:
        for key in ("updated_at","created_at"):
            try:
                stamp=datetime.fromisoformat(str(row[key]).replace("Z","+00:00"))
                if 0<=(now-stamp).total_seconds()<=14*86400:return True
            except (KeyError,ValueError,TypeError):pass
    return False


def expand_for_queries(sources, queries, budget, work_sources=()):
    """Read full relevant originals/sections after Light's semantic queries."""
    interest=terms(" ".join(queries));selected=[];used=0
    for original in getattr(sources,"originals",{}).values():
        if not isinstance(original,dict) or original.get("version")!="project-context-v1":continue
        for group in ("project","documents","decisions","tasks","planningGoals","openItems"):
            for row in ([original["project"]] if group=="project" else original.get(group) or []):
                from .redaction import redact
                entire=redact(encoded(row));score=len(terms(entire)&interest)
                if score and len(entire)<=budget//2:
                    selected.append((score,{"project_id":original["project"]["id"],"group":group,"id":row["id"],"field":"$",
                        "start":0,"end":len(entire),"total_characters":len(entire),"text":entire,"row_content_sha256":digest(row)}))
                    continue
                for field in ("name","title","content","notes","description","detail"):
                    text=row.get(field)
                    if not isinstance(text,str):continue
                    text=redact(text)
                    for start,end,part in sections(text,max(400,budget//2)):
                        score=max(len(terms(part)&interest),1 if str(row.get("id")) in " ".join(queries) else 0)
                        if score:selected.append((score,{"project_id":original["project"]["id"],"group":group,"id":row["id"],"field":field,
                            "start":start,"end":end,"total_characters":len(text),"text":part,"row_content_sha256":digest(row)}))
    for uri,row in getattr(work_sources,"originals",{}).items():
        from .redaction import redact
        for start,end,part in sections(redact(row["original"]),max(400,budget//2)):
            score=len(terms(part)&interest)
            if score:selected.append((score,{"source_uri":uri,"role":row["role"],"kind":row["kind"],
                "start":start,"end":end,"total_characters":len(row["original"]),"text":part,"source_version":row["source_version"],
                "source_date":row["source_date"],"work_state":row.get("work_state"),"timestamp_basis":row.get("timestamp_basis"),
                "snapshot_stale":row.get("snapshot_stale",False),"superseded_by":row.get("superseded_by"),"authority":row.get("authority")}))
    output=[]
    for _,item in sorted(selected,key=lambda p:-p[0]):
        size=len(encoded(item))
        if used+size<=budget:output.append(item);used+=size
    for item in output:item["offset_basis"]="redacted_original"
    return {"items":output,"matching_sections":len(selected),"selected_sections":len(output),
            "complete":len(output)==len(selected),"omitted_are_available_from_originals":True}


def section_evidence(expanded,sources,config,now):
    from .store import EvidenceRecord,snippet_hash
    from .redaction import redact
    result={}
    for item in expanded["items"]:
        if "source_uri" in item:
            ref="section:"+digest({k:v for k,v in item.items() if k!="text"})[:40];item["ref"]=ref
            quote=redact(item["text"])[:config.budgets.evidence_snippet_characters]
            provenance={k:item.get(k) for k in ("source_uri","source_version","source_date","work_state","timestamp_basis","snapshot_stale","superseded_by","authority")}
            provenance["evidence_status"]="historical" if item.get("snapshot_stale") or item.get("superseded_by") or item.get("work_state")=="historical_report" else "confirmed" if item["role"]=="user" else "unknown"
            result[ref]=EvidenceRecord(ref,"current-work",item["source_uri"],item["role"],item["source_date"][:10],quote,snippet_hash(quote),provenance)
            continue
        context=getattr(sources,"originals",{}).get(item["project_id"])
        if context is None:continue
        from . import project_context as native
        from .provenance import source_day
        kind=native.GROUPS.get(item["group"],"project")
        row=context["project"] if kind=="project" else next((r for r in context.get(item["group"]) or [] if r.get("id")==item["id"]),None)
        if row is None:continue
        source=next((s for s in context["sources"] if s["id"]==item["id"] and s.get("type")==kind),None)
        version=source.get("version") if source else None
        verified=isinstance(version,str) and len(version)==64 and set(version)<=set("e000000000000007")
        ref="section:"+digest({k:v for k,v in item.items() if k!="text"})[:40]
        item["ref"]=ref;item["source_version"]=version
        quote=redact(item["text"])[:config.budgets.evidence_snippet_characters]
        current=0 <= (now-datetime.fromisoformat(context["capturedAt"].replace("Z","+00:00"))).total_seconds() <= 86400
        provenance={"source":native.SOURCE,"source_uri":f"monolith:{item['group']}:{item['id']}:{item['field']}:{item['start']}:{item['end']}",
            "source_version":version,"source_date":source_day(row.get("updated_at") or row.get("created_at")),"captured_at":context["capturedAt"],
            "state_observed_at":context["capturedAt"],"entity_type":kind,"project_id":item["project_id"],
            "closed":native.closed(row) or native.closed(context["project"]),"current":current,"coverage_complete":context["coverage"]["complete"],
            "evidence_status":"confirmed" if verified and context["coverage"]["complete"] and current else "unknown",
            "coverage":{"start":item["start"],"end":item["end"],"total":item["total_characters"]},
            "authority":"original_section_not_new_user_rule_or_permission"}
        role="canonical" if provenance["evidence_status"]=="confirmed" else "assistant"
        result[ref]=EvidenceRecord(ref,"monolith",item["project_id"],role,provenance["source_date"] or "unknown",quote,snippet_hash(quote),provenance)
    return result


def archive_original(config,original):
    """One private content-addressed original, reused across rereads; no queue."""
    body={k:v for k,v in original.items() if k!="capturedAt"}
    key=digest(body)
    directory=config.staging_dir/"context-originals"
    if directory.is_symlink():raise ValueError("original_archive_symlink")
    directory.mkdir(parents=True,exist_ok=True);directory.chmod(0o700)
    path=directory/(key+".json")
    packet={"version":"dream-original/v1","content_sha256":key,"first_captured_at":original["capturedAt"],
        "required_scopes":original["requiredScopes"],"original":original}
    if path.is_symlink():raise ValueError("original_archive_symlink")
    if not path.exists():
        with path.open("x") as stream:json.dump(packet,stream,ensure_ascii=False)
        path.chmod(0o600)
    else:
        existing=json.loads(path.read_text())
        if existing.get("content_sha256")!=key or digest({k:v for k,v in existing["original"].items() if k!="capturedAt"})!=key:
            raise ValueError("original_archive_corrupt")
    # Only our own marker-bearing content-addressed originals in this folder.
    files=sorted(directory.glob("*.json"),key=lambda p:p.stat().st_mtime,reverse=True)
    for old in files[max(2,config.retain_runs):]:
        if old==path or old.is_symlink():continue
        if not re.fullmatch(r"[0-9a-f]{64}",old.stem):continue
        try:data=json.loads(old.read_text())
        except (ValueError,OSError):continue
        if data.get("version")=="dream-original/v1":old.unlink()
    return {"archive_id":key,"required_scopes":original["requiredScopes"],"full_original_retained":True,
            "cached_copy_is_not_new_read_permission":True}


def read_original(config,archive_id,*,current_scopes):
    if not re.fullmatch(r"[0-9a-f]{64}",archive_id):raise ValueError("original_id_invalid")
    path=config.staging_dir/"context-originals"/(archive_id+".json")
    if path.is_symlink():raise ValueError("original_archive_symlink")
    packet=json.loads(path.read_text())
    if not set(packet["required_scopes"])<=set(current_scopes):raise PermissionError("current_source_scopes_required")
    original=packet["original"]
    if digest({k:v for k,v in original.items() if k!="capturedAt"})!=archive_id:raise ValueError("original_archive_corrupt")
    return original


def add_telegram_originals(sources,sessions,config):
    """Resolve admitted message refs read-only, recheck full redacted version.

    Human authority stays exactly as classified by the existing bound scanner;
    a role=user database row never upgrades an unbound capsule here.
    """
    from .redaction import redact
    profiles={profile.name:profile for profile in config.enabled_profiles()}
    for session in sessions:
        profile=profiles.get(session.profile)
        if profile is None:continue
        try:
            with sqlite3.connect(profile.state_db.as_uri()+"?mode=ro",uri=True) as db:
                for message in session.messages:
                    suffix=message.ref.rsplit("#m",1)[-1]
                    if not suffix.isdigit():continue
                    row=db.execute("SELECT content,timestamp,active FROM messages WHERE session_id=? AND id=?",(session.session_id,int(suffix))).fetchone()
                    if not row or not row[2]:continue
                    text=redact(str(row[0]).strip())
                    if hashlib.sha256(text.encode()).hexdigest()!=message.content_hash:
                        sources.issues.append({"reason":"telegram_original_changed_since_scan"});continue
                    sources.originals[message.ref]={"id":message.ref,"source_uri":"hermes-message:"+message.ref,
                        "kind":"telegram_original","role":message.role,"original":text,"source_version":message.content_hash,
                        "source_date":datetime.fromtimestamp(row[1],timezone.utc).isoformat(),
                        "authority":"existing_scanner_human_binding_preserved","work_state":"conversation_evidence_not_new_permission",
                        "conversation_window_complete":False,"full_admitted_message_available":True}
        except (OSError,sqlite3.Error,ValueError,TypeError):
            sources.issues.append({"reason":"telegram_original_source_unavailable"})
