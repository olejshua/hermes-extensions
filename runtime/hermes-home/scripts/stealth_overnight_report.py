#!/usr/bin/env python3
"""One-shot, read-only recap of Stealth Campus overnight research checkpoints."""
import json
import re
from pathlib import Path

ROOT = Path('/home/hermes/.hermes/lead-research/2026-10-01')
LABELS = {'corporates': 'Konzerne', 'corporates-w2': 'Konzerne', 'family-offices': 'Family Offices', 'family-offices-w2': 'Family Offices', 'law-firms': 'Kanzleien', 'law-firms-w2': 'Kanzleien'}


def page_key(s):
    match = re.search(r'([0-9a-f]{32})', str(s).lower().replace('-', ''))
    return match.group(1) if match else ''


def records(path):
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                entries.append(obj)
        except json.JSONDecodeError:
            continue
    return entries


def value(obj, *keys):
    for key in keys:
        if obj.get(key):
            return str(obj[key])
    return ''


parts = ['Stealth-Recherche (1. Oktober):']
seen_pages = set()
seen_emails = set()
total = 0
work_count = 0
gmail_count = 0
groups = {label: {'rows': 0, 'work': 0, 'gmail': 0} for label in LABELS.values()}
for slug, label in LABELS.items():
    folder = ROOT / slug
    committed = records(folder / 'committed.jsonl')
    checkpoints = records(folder / 'checkpoint.jsonl')
    valid = []
    valid_ids = set()
    for entry in committed:
        marker = page_key(value(entry, 'notion_page_id', 'page_id', 'notion_id', 'notion_url', 'url', 'id'))
        if marker and marker not in seen_pages:
            seen_pages.add(marker)
            valid_ids.add(marker)
            valid.append(entry)
    total += len(valid)
    indexed = []
    for item in records(folder / 'email_index.jsonl'):
        marker = page_key(value(item, 'notion_url', 'url', 'page_id', 'notion_id'))
        email = value(item, 'email').strip().lower()
        confidence = value(item, 'confidence').lower()
        if marker not in valid_ids or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            continue
        if not confidence or item.get('strict_current') is False or confidence.startswith(('review_only', 'historical_only', 'index_only', 'guessed', 'inferred', 'unverified')):
            continue
        if email in seen_emails:
            continue
        seen_emails.add(email)
        indexed.append(email)
    gmail = sum(email.endswith('@gmail.com') for email in indexed)
    work_count += len(indexed) - gmail
    gmail_count += gmail
    groups[label]['rows'] += len(valid)
    groups[label]['work'] += len(indexed) - gmail
    groups[label]['gmail'] += gmail
if total == 0:
    parts.append('Noch keine per Readback dokumentierten neuen Notion-Einträge; lokale Checkpoints prüfen.')
else:
    for label in ('Konzerne', 'Family Offices', 'Kanzleien'):
        g = groups[label]
        parts.append(f'• {label}: {g["rows"]} neue CRM-Zeilen, {g["work"]} persönliche Arbeitsadressen' + (f', {g["gmail"]} berufliche Gmail' if g['gmail'] else '') + '.')
    parts.append(f'Gesamt: {total} neue CRM-Zeilen, {work_count} aktuell öffentlich belegte persönliche Arbeitsadressen' + (f' und {gmail_count} beruflich veröffentlichte Gmail-Adresse(n)' if gmail_count else '') + '. Drei historische bzw. nur indexierte Family-Office-Adressen sind NICHT mitgezählt.')
    parts.append('Eigentümerschaft und Zustellbarkeit vor Outreach prüfen. Keine Entwürfe, kein Versand, kein Reset.')
parts.append('Notion: https://app.notion.com/p/e000000000000000000000000000000a')
print('\n'.join(parts))
