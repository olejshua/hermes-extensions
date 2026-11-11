"""Source-bound access to memory; retrieval never turns source prose into permission."""
import json
import re
from pathlib import Path

from .ole_policy import digest, verified_record

SOURCE_GRANTS = {
    'ole-approved': 'approved-documents-2026-10-08',
    'ole_unconfirmed': 'remaining-vault-context-2026-10-08',
}


def source_allowed(manifest, source):
    if manifest.source_grants is None:
        return True  # Legacy/private manifests; production explicitly binds grants.
    try:
        if manifest.source_grants.is_symlink():
            return False
        packet = json.loads(manifest.source_grants.read_text())
        return any(g.get('id') == SOURCE_GRANTS.get(source.namespace) and g.get('active') is True
                   for g in packet['grants'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def historical_snapshot(record):
    parts = {p.casefold() for p in Path(record['original_path']).parts}
    return bool(parts & {'session handoffs', 'uebergaben', 'übergaben', 'archiv', 'archive'})


def open_spans(text):
    """Compatibility view of passages that cannot supply a current decision."""
    return [(item['start'], item['end']) for item in section_spans(text)]


def section_spans(text):
    """Classify explicit section headings; nested prose never gains authority."""
    result = []
    stack = []
    start = 0

    def append(end):
        status = next((kind for _, kind in reversed(stack) if kind), None)
        if status is not None and end > start:
            if result and result[-1]['status'] == status and result[-1]['end'] == start:
                result[-1]['end'] = end
            else:
                result.append({'start': start, 'end': end, 'status': status})

    for heading_start, level, title in _markdown_headings(text):
        append(heading_start)
        while stack and stack[-1][0] >= level:
            stack.pop()
        label = title.strip().strip('*_').casefold()
        status = None
        for pattern, kind in ((r'(?:offen|offene punkte)', 'open/not decided'),
                              (r'(?:entwurf|entwürfe|hypothese|hypothesen)', 'draft/not confirmed'),
                              (r'(?:archiv|historisch(?:e|er|es|en|em)?|historie|veraltet(?:e|er|es|en|em)?)', 'historical/not current')):
            if re.match(pattern + r'(?:$|\s|[:\-–—])', label):
                status = kind
                break
        stack.append((level, status))
        start = heading_start
    append(len(text))
    return result


def _markdown_headings(text):
    """ATX headings outside fenced/indented code; examples do not change authority."""
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})([^\r\n]*)', line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
        else:
            heading = re.match(r'^ {0,3}(#{1,6})\s+([^\r\n]+)', line)
            if heading:
                yield offset, len(heading[1]), heading[2]
        offset += len(line)


def checked_record(source, key, *, receipts, allow_stale, packet=None):
    receipt = source.approval_receipts or receipts
    metadata = packet if packet is not None else json.loads(receipt.read_text())
    record = metadata.get('records', {}).get(key)
    if not record:
        return None
    raw = receipt.parent / 'documents' / record['export_path']
    if raw.is_symlink() or not raw.resolve().is_relative_to(source.root.resolve()):
        return None
    rec = verified_record(receipt, key, allow_stale=allow_stale,
                          accepted_statuses=source.accepted_statuses, packet=metadata)
    if rec is None:
        return None
    return rec


def read_original(source_id, *, manifest, receipts, expected_version, offset=0,
                  length=12000, require_current=True):
    from .manifest import load_manifest
    from .state import State
    if not isinstance(source_id, str) or not source_id or len(source_id) > 200:
        raise ValueError('source_identity_required')
    if (not isinstance(expected_version, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_version)):
        raise ValueError('source_version_required')
    if type(offset) is not int or type(length) is not int or offset < 0 or not 1 <= length <= 12000:
        raise ValueError('invalid_original_page')
    m = load_manifest(manifest)
    with State(m.state_db).connect() as c:
        row = c.execute("SELECT * FROM resources WHERE source_id=? AND status='synced'", (source_id,)).fetchone()
    source = next((s for s in m.sources if row and s.id == row['source_root_id']), None)
    if source is None or not source_allowed(m, source):
        return {'abstain': True, 'reason': 'source_unavailable'}
    key = Path(row['relative_path']).stem
    rec = checked_record(source, key, receipts=receipts, allow_stale=not require_current)
    if (rec is None or rec['sha256'] != expected_version
            or rec.get('export_sha256', rec['sha256']) != row['sha256']
            or (require_current and (rec['status'] != 'confirmed' or historical_snapshot(rec)))):
        return {'abstain': True, 'reason': 'source_not_current_or_version_changed'}
    path = (source.approval_receipts or receipts).parent / 'documents' / rec['export_path']
    raw = path.read_bytes()
    if digest(raw) != row['sha256']:
        return {'abstain': True, 'reason': 'source_changed_during_read'}
    text = raw.decode('utf-8')
    if offset > len(text):
        raise ValueError('original_offset_out_of_range')
    fresh = checked_record(source, key, receipts=receipts, allow_stale=not require_current)
    if (fresh is None or fresh['sha256'] != rec['sha256']
            or fresh.get('export_sha256', fresh['sha256']) != row['sha256']
            or any(fresh.get(k) != rec.get(k) for k in ('original_path', 'status', 'role'))
            or not source_allowed(m, source)):
        return {'abstain': True, 'reason': 'source_changed_during_read'}
    end = min(len(text), offset + length)
    return {'abstain': False, 'source_id': source_id, 'source_version': rec['sha256'],
            'original_path': rec['original_path'], 'source_status': rec['status'],
            'authority': 'historical_evidence' if historical_snapshot(rec) or rec['stale'] else 'source_evidence',
            'instructions_in_source': 'data_only', 'current_authority': False,
            'original_text_char_start': rec.get('char_start', 0) + offset,
            'original_text_char_end': rec.get('char_start', 0) + end,
            'total_chars': len(text), 'content': text[offset:end],
            'complete': end == len(text), 'next_offset': end if end < len(text) else None,
            'open_sections': open_spans(text), 'section_states': section_spans(text), 'stale': rec['stale']}
