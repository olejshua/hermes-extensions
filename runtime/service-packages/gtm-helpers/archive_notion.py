#!/usr/bin/env python3
"""Archive a completed meeting; preview by default, writes require --write.

Only the fixed Acribic Meetings parent is allowed. Writes are never retried.
Credentials and recovery state stay outside the Vault.
"""
import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

PARENT = '00000000-0000-4000-8000-000000000010'
API = 'https://api.notion.com/v1/'
STATE = Path.home() / '.cache/acribic/meetings'


def quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'


def request(key, method, path, body=None):
    config = 'header = ' + quoted('Authorization: Bearer ' + key) + '\n'
    config += 'header = "Notion-Version: 2022-06-28"\nheader = "Content-Type: application/json"\n'
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False)
        if len(payload.encode()) >= 450000:
            raise RuntimeError('Payload exceeds local size limit')
        config += 'data = ' + quoted(payload) + '\n'
    p = subprocess.run(['curl', '--silent', '--show-error', '--max-time', '30',
                        '--config', '-', '--request', method, API + path,
                        '--write-out', '\n%{http_code}'], input=config,
                       text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError('Network result uncertain; read destination before another write')
    raw, status = p.stdout.rsplit('\n', 1)
    result = json.loads(raw)
    if not 200 <= int(status) < 300:
        raise RuntimeError('Notion HTTP ' + status + ' (' + str(result.get('code', 'error')) + ')')
    return result


def children(key, parent):
    rows, cursor, seen = [], None, set()
    while True:
        params = {'page_size': 100}
        if cursor:
            params['start_cursor'] = cursor
        data = request(key, 'GET', 'blocks/' + parent + '/children?' + urlencode(params))
        rows.extend(data['results'])
        if not data['has_more']:
            return rows
        cursor = data.get('next_cursor')
        if not cursor or cursor in seen:
            raise RuntimeError('Incomplete Notion pagination')
        seen.add(cursor)
        time.sleep(.4)


def text_block(kind, content):
    return {'object': 'block', 'type': kind, kind: {'rich_text': [
        {'type': 'text', 'text': {'content': content}}]}}


def chunks(text):
    start, units = 0, 0
    for i, char in enumerate(text):
        width = 2 if ord(char) > 0xffff else 1
        if units + width > 1800:
            yield text[start:i]
            start, units = i, 0
        units += width
    if start < len(text):
        yield text[start:]


def document_blocks(text):
    # The transcript is opaque text: heading-like source speech is not parsed.
    before, marker, transcript = text.partition('\n## Originaltranskript\n')
    if not marker or not transcript.strip():
        raise RuntimeError('Document needs a nonempty final section: ## Originaltranskript')
    if transcript.startswith('\n'):
        transcript = transcript[1:]  # Remove the section's formatting separator.
    result = []
    for paragraph in before.split('\n\n'):
        heading = re.fullmatch(r'(#{1,3}) (.+)', paragraph)
        kind = 'heading_' + str(len(heading[1])) if heading else 'paragraph'
        value = heading[2] if heading else paragraph
        result.extend(text_block(kind, part) for part in chunks(value))
    result.append(text_block('heading_2', 'Originaltranskript'))
    result.extend(text_block('paragraph', part) for part in chunks(transcript))
    return result


def signature(block):
    kind = block['type']
    if kind not in ('paragraph', 'heading_1', 'heading_2', 'heading_3') or block.get('has_children'):
        raise RuntimeError('Unexpected existing content; do not overwrite')
    return kind, ''.join(t.get('plain_text', t.get('text', {}).get('content', ''))
                         for t in block[kind].get('rich_text', []))


def matching_prefix(actual, expected):
    if len(actual) > len(expected) or [signature(b) for b in actual] != [signature(b) for b in expected[:len(actual)]]:
        raise RuntimeError('Existing content differs; reconcile or create an explicit revision')
    return len(actual)


def read_state(path):
    if not path.exists():
        return {}
    return {key: value for key, value in re.findall(r'^- ([a-z_]+): (.*)$', path.read_text(), re.M)}


def save_state(path, values):
    data = '# Notion meeting archive\n\n' + ''.join('- ' + k + ': ' + v + '\n' for k, v in values.items())
    temporary = path.with_suffix('.partial')
    temporary.write_text(data)
    temporary.chmod(0o600)
    temporary.replace(path)


def find_page(key, title, meeting_id, state):
    if state.get('page_id'):
        page = request(key, 'GET', 'pages/' + state['page_id'])
        if page.get('parent', {}).get('page_id', '').replace('-', '') != PARENT.replace('-', '') or page.get('archived') or page.get('in_trash'):
            raise RuntimeError('Journal page has wrong parent or is archived')
        return page['id']
    matches = []
    for row in children(key, PARENT):
        if row['type'] == 'child_page' and row['child_page']['title'] == title:
            existing = children(key, row['id'])
            if existing and signature(existing[0]) == ('paragraph', 'Wispr-ID: ' + meeting_id):
                matches.append(row['id'])
            else:
                raise RuntimeError('Same-title page has different/unknown identity; clarify')
    if len(matches) > 1:
        raise RuntimeError('Multiple pages for this meeting; clarify without deleting')
    if matches:
        return matches[0]
    if state.get('create_attempted') == 'yes':
        raise RuntimeError('Earlier create is unresolved; do not create a second page')
    return None


def archive(key, meeting_id, title, body, state_path, write=False):
    parent = request(key, 'GET', 'pages/' + PARENT)
    if parent.get('archived') or parent.get('in_trash'):
        raise RuntimeError('Meetings parent is archived')
    digest = hashlib.sha256(body.encode()).hexdigest()
    expected = [text_block('paragraph', 'Wispr-ID: ' + meeting_id),
                text_block('paragraph', 'Archiv-SHA256: ' + digest)] + document_blocks(body)
    state = read_state(state_path)
    if state.get('sha256') not in (None, digest) or state.get('title') not in (None, title):
        raise RuntimeError('Archived source/title changed; use an explicit revision')
    page_id = find_page(key, title, meeting_id, state)
    count = matching_prefix(children(key, page_id), expected) if page_id else 0
    if state.get('append_expected_end'):
        if count < int(state['append_expected_end']):
            raise RuntimeError('Earlier append remains unresolved; do not repeat missing blocks')
        state.pop('append_attempt_at', None)
        state.pop('append_expected_end', None)
    if not write:
        return {'mode': 'preview', 'action': 'create' if not page_id else 'complete' if count == len(expected) else 'resume',
                'page_id': page_id, 'blocks': len(expected), 'missing_blocks': len(expected) - count}
    state.update(title=title, sha256=digest, meeting_id=meeting_id)
    if not page_id:
        state['create_attempted'] = 'yes'
        save_state(state_path, state)
        created = request(key, 'POST', 'pages', {
            'parent': {'page_id': PARENT},
            'properties': {'title': {'type': 'title', 'title': [{'type': 'text', 'text': {'content': title}}]}},
            'children': expected[:40]})
        page_id = created['id']
    state['page_id'] = page_id
    save_state(state_path, state)
    count = matching_prefix(children(key, page_id), expected)
    while count < len(expected):
        state['append_attempt_at'] = str(count)
        state['append_expected_end'] = str(min(count + 40, len(expected)))
        save_state(state_path, state)
        time.sleep(.4)
        batch = expected[count:count + 40]
        request(key, 'PATCH', 'blocks/' + page_id + '/children', {'children': batch})
        new_count = matching_prefix(children(key, page_id), expected)
        if new_count != count + len(batch):
            raise RuntimeError('Append readback mismatch; inspect before resuming')
        count = new_count
        state.pop('append_attempt_at', None)
        state.pop('append_expected_end', None)
        save_state(state_path, state)
    state['complete'] = 'yes'
    save_state(state_path, state)
    return {'mode': 'verified', 'page_id': page_id,
            'url': 'https://www.notion.so/' + page_id.replace('-', ''), 'blocks': count}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--meeting-id', required=True)
    p.add_argument('--title', required=True)
    p.add_argument('--document', type=Path, required=True)
    p.add_argument('--write', action='store_true')
    args = p.parse_args()
    if not args.meeting_id.strip() or '\n' in args.meeting_id or not args.title.strip() or '\n' in args.title or len(args.title.encode('utf-16-le')) > 3600:
        p.error('Invalid meeting identity/title')
    body = args.document.read_text()
    if body.startswith('---\n'):
        _, separator, body = body[4:].partition('\n---\n')
        if not separator:
            p.error('Unclosed frontmatter')
        body = body.removeprefix('\n')
    key = (Path.home() / '.config/acribic/notion-token').read_text().strip()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path = STATE / ('notion-' + hashlib.sha256(args.meeting_id.encode()).hexdigest()[:24] + '.md')
    with (STATE / 'notion-archive.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = archive(key, args.meeting_id, args.title, body, state_path, args.write)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
