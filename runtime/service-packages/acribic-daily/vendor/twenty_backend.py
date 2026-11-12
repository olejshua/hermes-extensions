#!/usr/bin/env python3
"""Live Twenty source and write adapter for the existing Acribic safety gates.

Notion IDs are provenance identifiers only. No request goes to Notion.
Complete native companies, people, and review cases join the suppression base.
"""
import collections
import copy
import datetime as dt
import json
import os
from pathlib import Path
import time
from notion_twenty_migration import TWENTY_URL, graphql, request_json, token, write_once
from notion_twenty_test3 import stable_id
from twenty_review_resolution import validate_resolved

LISTS = ('it', 'ingenieur', 'medizin', 'beratung', 'sicherheit', 'reinigung', 'icp')
POLICY_ID = stable_id('outreach-policy', 'acribic-twenty-v1')
FIELDS = {
    'company': 'id updatedAt name domainName { primaryLinkUrl primaryLinkLabel } address { addressCity } kontaktiert kontaktiertAm notionQuellen notionSeitenIds notionListen notionStages notionEmails notionVersandbereit outreachHistorie',
    'review': 'id updatedAt name notionPageId notionListe notionRohdaten notionStage notionEmail notionLastEdited kandidaten versandbereit outreachHistorie importStatus',
    'person': 'id updatedAt name { firstName lastName } emails { primaryEmail additionalEmails } companyId mailErlaubt',
}
OBJECTS = {'company': ('companies', 'Company', 'company'),
           'review': ('notionImportCases', 'NotionImportCase', 'notionImportCase'),
           'person': ('people', 'Person', 'person')}


def read_record(key, kind, rid):
    plural, _, singular = OBJECTS[kind]
    return request_json('GET', f'{TWENTY_URL}/rest/{plural}/{rid}', key)['data'][singular]


def mutate(key, kind, rid, data, journal):
    _, title, _ = OBJECTS[kind]
    journal(dict(event='crm_attempt', kind=kind, id=rid, fields=data))
    result = write_once('POST', f'{TWENTY_URL}/graphql', key,
        dict(query=f'mutation($id:UUID!,$data:{title}UpdateInput!) {{ update{title}(id:$id,data:$data) {{ id }} }}',
             variables=dict(id=rid, data=data)))
    if result.get('errors') or (result.get('data') or {}).get('update'+title, {}).get('id') != rid:
        raise RuntimeError('Unclear Twenty write; read/reconcile, never automatically retry')
    live = read_record(key, kind, rid)
    if any(live.get(k) != v for k, v in data.items()):
        raise RuntimeError('Twenty write readback mismatch')
    journal(dict(event='crm_readback', kind=kind, id=rid))
    return live


def paginated(key, kind, since=None):
    plural, title, _ = OBJECTS[kind]
    query = f'''query($first:Int!,$after:String,$filter:{title}FilterInput) {{
        {plural}(first:$first,after:$after,filter:$filter) {{ totalCount
        pageInfo {{ endCursor hasNextPage }} edges {{ node {{ {FIELDS[kind]} }} }} }} }}'''
    cursor, cursors, ids, rows, expected = None, set(), set(), [], None
    while True:
        variables = dict(first=1000, after=cursor)
        if since:
            variables['filter'] = {'updatedAt': {'gte': since}}
        data = graphql(key, query, variables)[plural]
        if expected is None:
            expected = data['totalCount']
        if data['totalCount'] != expected:
            raise RuntimeError('Twenty count changed during pagination')
        for edge in data['edges']:
            row = edge['node']
            if row['id'] in ids:
                raise RuntimeError('Repeated Twenty record in pagination')
            ids.add(row['id']); rows.append(row)
        if not data['pageInfo']['hasNextPage']:
            break
        cursor = data['pageInfo'].get('endCursor')
        if not cursor or cursor in cursors:
            raise RuntimeError('Incomplete Twenty pagination')
        cursors.add(cursor)
        time.sleep(.12)
    if len(rows) != expected:
        raise RuntimeError('Incomplete Twenty record count')
    return rows


def summaries(sources):
    effective = lambda s: {**s['properties'], **s.get('outreach', {})}
    return dict(notionSeitenIds='; '.join(s['page_id'] for s in sources),
                notionListen='; '.join(sorted({s['liste'] for s in sources})),
                notionStages='; '.join(sorted({str(effective(s).get('Stage') or '') for s in sources if effective(s).get('Stage')})),
                notionEmails='; '.join(sorted({str(effective(s).get('E-Mail') or '').strip() for s in sources if effective(s).get('E-Mail')})))


def read_ids(key, kind, ids):
    plural, title, _ = OBJECTS[kind]
    query = f'query($filter:{title}FilterInput!) {{ {plural}(first:1000,filter:$filter) {{ totalCount edges {{ node {{ {FIELDS[kind]} }} }} }} }}'
    result = graphql(key, query, {'filter': {'id': {'in': ids}}})[plural]
    rows = {edge['node']['id']: edge['node'] for edge in result['edges']}
    if set(rows) != set(ids) or result['totalCount'] != len(ids):
        raise RuntimeError('Target read is incomplete')
    return rows


def update_batch(key, kind, payloads, journal):
    """Existing-record updates only; verify every alias after a single write."""
    if not payloads or len(payloads) > 30 or len({p['id'] for p in payloads}) != len(payloads):
        raise RuntimeError('Invalid CRM update batch')
    _, title, _ = OBJECTS[kind]
    journal(dict(event='crm_batch_attempt', kind=kind, payloads=payloads))
    # This Twenty installation rejects repeated root resolvers, even with aliases.
    # Use update-only calls, paced below the workspace rate limit.
    for payload in payloads:
        result = write_once('POST', f'{TWENTY_URL}/graphql', key,
            dict(query=f'mutation($id:UUID!,$data:{title}UpdateInput!) {{ update{title}(id:$id,data:$data) {{ id }} }}',
                 variables=dict(id=payload['id'],data=payload['data'])))
        if result.get('errors'):
            journal(dict(event='crm_batch_error',kind=kind,id=payload['id'],errors=result['errors']))
            raise RuntimeError(f'Unclear update; read before recovery: {result["errors"]}')
        if (result.get('data') or {}).get('update'+title, {}).get('id') != payload['id']:
            raise RuntimeError('Unexpected mutation ID')
        journal(dict(event='crm_update_ack',kind=kind,id=payload['id']))
        time.sleep(.75)
    live = read_ids(key, kind, [p['id'] for p in payloads])
    for payload in payloads:
        if any(live[payload['id']].get(k) != v for k,v in payload['data'].items()):
            raise RuntimeError('CRM batch field readback mismatch')
    journal(dict(event='crm_batch_readback', kind=kind, ids=list(live)))
    return live


def property_wrapper(value):
    if isinstance(value, bool):
        return dict(type='checkbox', checkbox=value)
    if isinstance(value, (int, float)):
        return dict(type='number', number=value)
    return dict(type='rich_text', rich_text=[{'plain_text': str(value or '')}])


class TwentyBackend:
    def __init__(self, state):
        self.key = token('twenty-api-key')
        self.state = Path(state)
        self.records = {}
        self.source_map = {}
        self.policy = None
        self.anchor = None

    def journal(self, event):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.state / 'twenty-crm-journal.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as out:
            out.write(json.dumps(dict(at=dt.datetime.now(dt.timezone.utc).isoformat(), **event), ensure_ascii=False)+'\n')
            out.flush(); os.fsync(out.fileno())

    def read_policy(self):
        note = request_json('GET', f'{TWENTY_URL}/rest/notes/{POLICY_ID}', self.key)['data']['note']
        markdown = (note.get('bodyV2') or {}).get('markdown') or ''
        if not markdown.startswith('ACRIBIC_TWENTY_POLICY_V1\n'):
            raise RuntimeError('Twenty outreach policy missing/invalid')
        policy = json.loads(markdown.split('\n', 1)[1])
        if policy.get('version') != 1 or set(policy.get('list_counts', {})) != set(LISTS):
            raise RuntimeError('Seven-list Twenty policy incomplete')
        for field in ('suppression_text', 'sent_history_csv', 'attempted_mailboxes'):
            if field not in policy:
                raise RuntimeError('Twenty protection source missing')
        self.policy = policy
        return policy

    def refresh(self, complete=False):
        # The anchor precedes the baseline, so edits during pagination are captured.
        if not self.records or complete:
            # Twenty accepts UTC seconds with Z, without fractional seconds or
            # +00:00. Round down so changes in this first second are included.
            self.anchor = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            self.records = {kind: {r['id']:r for r in paginated(self.key, kind)} for kind in OBJECTS}
        # Replay changes before validating even the first baseline. Resolution
        # writes the company before marking its review resolved; read reviews
        # first here so their company evidence is fetched afterwards. The same
        # strict source/history validation still rejects incomplete live data.
        for kind in ('review', 'company', 'person'):
            for row in paginated(self.key, kind, self.anchor):
                self.records[kind][row['id']] = row
        self.read_policy()
        return self.groups()

    def groups(self):
        groups = {name: [] for name in LISTS}
        mapping = {}
        resolved = {r['id'] for r in self.records.get('review', {}).values()
                    if validate_resolved(r, self.records.get('company', {}))}
        candidate_holds = {c['id'] for r in self.records.get('review', {}).values()
                           if r['id'] not in resolved for c in r.get('kandidaten') or []}
        for kind in ('company', 'review'):
            for target in self.records.get(kind, {}).values():
                if kind == 'review' and target['id'] in resolved:
                    continue
                sources = target.get('notionQuellen') or [] if kind == 'company' else [target['notionRohdaten']]
                for source in sources:
                    pid = source['page_id'].replace('-', '').lower()
                    if pid in mapping or source['liste'] not in groups:
                        raise RuntimeError('Duplicate source identity or unknown Twenty segment')
                    mapping[pid] = (kind, target['id'])
                    history = target.get('outreachHistorie') or []
                    blocked = (kind != 'company' or target.get('notionVersandbereit') is not True
                               or target.get('kontaktiert') is True or bool(target.get('kontaktiertAm'))
                               or bool(history) or target['id'] in candidate_holds)
                    row = dict({**source['properties'], **source.get('outreach', {})}, id=pid, _TwentyBlocked=blocked,
                               _TwentyKind=kind, _TwentyId=target['id'])
                    groups[source['liste']].append(row)
                if kind == 'company' and not sources:
                    # Native imported leads are not silently promoted to first contact.
                    row = dict(id=target['id'].replace('-', ''), Firma=target['name'],
                               Website=(target.get('domainName') or {}).get('primaryLinkUrl') or '',
                               Stage='Twenty ungeprüft', _TwentyBlocked=True, _TwentyKind='native', _TwentyId=target['id'])
                    groups['icp'].append(row)
        for person in self.records.get('person', {}).values():
            emails = person.get('emails') or {}
            for i, email in enumerate([emails.get('primaryEmail')] + (emails.get('additionalEmails') or [])):
                if not email:
                    continue
                company = self.records.get('company', {}).get(person.get('companyId')) or {}
                name = company.get('name') or ' '.join((person.get('name') or {}).values())
                groups['icp'].append(dict(id=stable_id('person-mailbox', person['id']+str(i)).replace('-', ''),
                    Firma=name, **{'E-Mail': email}, Stage='Twenty Person ungeprüft', _TwentyBlocked=True))
        expected = self.policy['list_counts'] if self.policy else None
        if expected is not None:
            counts = collections.Counter(r['liste'] for kind in ('company','review') for target in self.records[kind].values()
                for r in (target.get('notionQuellen') or [] if kind == 'company' else
                          [] if target['id'] in resolved else [target['notionRohdaten']]))
            if any(counts[name] < count for name,count in expected.items()):
                raise RuntimeError('Twenty imported seven-list source coverage incomplete')
        self.source_map = mapping
        return groups

    def row(self, pid):
        pid = pid.replace('-', '').lower()
        if pid not in self.source_map:
            self.refresh(complete=True)
        kind, rid = self.source_map[pid]
        live = read_record(self.key, kind, rid)
        self.records[kind][rid] = live
        groups = self.groups()
        rows = [r for rows in groups.values() for r in rows if r['id'] == pid]
        if len(rows) != 1:
            raise RuntimeError('Selected Twenty source is not unique')
        return rows[0]

    def page(self, pid):
        row = self.row(pid)
        return dict(archived=False, in_trash=False, properties={k:property_wrapper(v) for k,v in row.items() if k != 'id'})

    def patch(self, pid, stage=None, note=None):
        row = self.row(pid)
        kind, rid = self.source_map[row['id']]
        if kind != 'company':
            raise RuntimeError('Review/native targets cannot be used for outreach writes')
        live = self.records[kind][rid]
        sources = copy.deepcopy(live['notionQuellen'])
        selected = [s for s in sources if s['page_id'].replace('-', '') == row['id']]
        if len(selected) != 1:
            raise RuntimeError('Source identity changed')
        props = selected[0].setdefault('outreach', {})
        previous = {**selected[0]['properties'], **props}
        if stage:
            props['Stage'] = stage
        if note is not None:
            props['Notizen'] = note + (' | '+str(previous.get('Notizen')) if previous.get('Notizen') else '')
        data = dict(notionQuellen=sources, **summaries(sources))
        if stage and stage != 'Nicht kontaktiert':
            data['notionVersandbereit'] = False
        if stage == 'E-Mail raus':
            data['kontaktiert'] = True
            if not live.get('kontaktiertAm'):
                data['kontaktiertAm'] = dt.date.today().isoformat()
        self.records[kind][rid] = mutate(self.key, kind, rid, data, self.journal)
        from notion_twenty_test3 import note_markdown
        source = selected[0]
        markdown = note_markdown({k:v for k,v in source.items() if k != 'outreach'})
        markdown += '\n## Aktueller Twenty-Stand\n\n' + '\n\n'.join(f'**{k}:** {v}' for k,v in props.items()) + '\n'
        note_id = stable_id('note', source['page_id'])
        self.journal(dict(event='source_note_attempt',id=note_id))
        result = write_once('POST', f'{TWENTY_URL}/graphql', self.key,
            dict(query='mutation($id:UUID!,$data:NoteUpdateInput!) { updateNote(id:$id,data:$data) { id } }',
                 variables=dict(id=note_id,data={'bodyV2': {'markdown': markdown}})))
        if result.get('errors') or (result.get('data') or {}).get('updateNote',{}).get('id') != note_id:
            raise RuntimeError('Source note write uncertain; CRM stage may already be committed')
        note_readback = request_json('GET',f'{TWENTY_URL}/rest/notes/{note_id}',self.key)['data']['note']
        if (note_readback.get('bodyV2') or {}).get('markdown') != markdown:
            raise RuntimeError('Source note readback mismatch')
        self.journal(dict(event='source_note_readback',id=note_id))
