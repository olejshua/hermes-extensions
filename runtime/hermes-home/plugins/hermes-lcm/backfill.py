"""Explicit read-only historical Hermes transcript import, with atomic receipts.

Never runs on plugin load. Only human conversation lanes are selected by default;
cron, worker/subagent, oneshot, tool, hidden and internal-review sessions are excluded.
Originals remain authoritative in state.db. LCM receives redacted conversation evidence,
not approved personal memory. No source filesystem scans or memory publication.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
from pathlib import Path
from .ingest_protection import protect_message_for_ingest
from .store import _normalize_content_value
from .tokens import count_message_tokens

DEFAULT_SOURCES = ("cli", "desktop", "telegram")

def import_history(engine, source_db: str, *, allowed_sources=DEFAULT_SOURCES, session_ids=None, preview=False):
    source = Path(source_db).resolve(strict=True)
    if source == Path(engine._store.db_path).resolve():
        raise ValueError("Source and target databases must differ")
    if not set(allowed_sources).issubset(DEFAULT_SOURCES):
        raise ValueError("Import sources require an explicit reviewed importer extension")
    origin = hashlib.sha256(str(source).encode()).hexdigest()
    con = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # One coherent SQLite read snapshot, including committed WAL history.
    con.execute("BEGIN")
    slots = ",".join("?" for _ in allowed_sources)
    sessions = con.execute(f"SELECT id,source,session_key,parent_session_id,title FROM sessions WHERE source IN ({slots}) AND COALESCE(hidden,0)=0 ORDER BY started_at,id", tuple(allowed_sources)).fetchall()
    sessions = [s for s in sessions if (not session_ids or s['id'] in session_ids) and not any(x in (str(s['title'] or '')+' '+str(s['session_key'] or '')).lower() for x in ('background_review','background-review','internal_review','internal-review'))]
    counts = {"sessions": len(sessions), "imported": 0, "already_imported": 0, "preview": preview, "source_fingerprint": origin, "evidence_status": "historical_conversation", "excluded_sources": ["cron", "subagent", "oneshot", "tool"], "rows_by_source": {}}
    target = engine._store._conn
    if not preview:
        with target:
            target.execute("""CREATE TABLE IF NOT EXISTS lcm_backfill_receipts (
              origin TEXT NOT NULL, source_message_id INTEGER NOT NULL, store_id INTEGER NOT NULL,
              source_session_id TEXT NOT NULL, source_timestamp REAL NOT NULL, source_platform TEXT NOT NULL,
              parent_session_id TEXT, project_scope TEXT NOT NULL DEFAULT 'conversation-only',
              evidence_status TEXT NOT NULL DEFAULT 'historical_conversation',
              PRIMARY KEY(origin,source_message_id))""")
            target.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lcm_backfill_store_id ON lcm_backfill_receipts(store_id)")
    try:
        for session in sessions:
            rows = con.execute("SELECT id,role,content,tool_call_id,tool_calls,tool_name,timestamp,token_count,_compressed_summary FROM messages WHERE session_id=? ORDER BY id", (session['id'],)).fetchall()
            # Existing compressed scaffolds are derived; preserve original rows instead.
            rows = [r for r in rows if not r['_compressed_summary'] and r['role'] in ('user','assistant','tool')]
            counts['rows_by_source'][session['source']] = counts['rows_by_source'].get(session['source'],0) + len(rows)
            if preview:
                counts['imported'] += len(rows)
                continue
            with engine._store._write_lock, target:
                for row in rows:
                    if target.execute("SELECT 1 FROM lcm_backfill_receipts WHERE origin=? AND source_message_id=?",(origin,row['id'])).fetchone():
                        counts['already_imported'] += 1
                        continue
                    calls = json.loads(row['tool_calls']) if row['tool_calls'] else None
                    raw = {"role": row['role'], "content": row['content'], "tool_call_id": row['tool_call_id'], "tool_calls": calls, "tool_name": row['tool_name']}
                    message = protect_message_for_ingest(raw, config=engine._config, hermes_home=engine._hermes_home, session_id=session['id'])
                    cur = target.execute("""INSERT INTO messages (session_id,source,conversation_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,token_estimate,pinned)
                      VALUES (?,?,?,?,?,?,?,?,?,?,0)""",(session['id'],session['source'],session['session_key'] or session['id'],message['role'],_normalize_content_value(message.get('content')),message.get('tool_call_id'),json.dumps(message['tool_calls']) if message.get('tool_calls') else None,message.get('tool_name'),row['timestamp'],count_message_tokens(message)))
                    target.execute("INSERT INTO lcm_backfill_receipts(origin,source_message_id,store_id,source_session_id,source_timestamp,source_platform,parent_session_id) VALUES (?,?,?,?,?,?,?)",(origin,row['id'],cur.lastrowid,session['id'],row['timestamp'],session['source'],session['parent_session_id']))
                    counts['imported'] += 1
    finally:
        con.close()
    return counts
