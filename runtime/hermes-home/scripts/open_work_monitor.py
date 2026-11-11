#!/usr/bin/env python3
"""Cheap nightly gate for user-assigned open work; no LLM if unchanged."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import re

inbox = Path('/home/hermes/vaults/obsidian/Codex/uebergaben/Hermes — Offene Aufträge.md')
if not inbox.exists():
    print('none')
    raise SystemExit(0)
body = inbox.read_text(encoding='utf-8')
section = body.split('## Offen\n', 1)[-1].split('\n## ', 1)[0]
items = re.findall(r'^- \[ \] \[([A-Za-z0-9-]+)\] (.+)$', section, re.MULTILINE)
if not items:
    print('none')
else:
    print('open-date=' + datetime.now(ZoneInfo('Europe/Berlin')).date().isoformat())
    for ident, text in items:
        print(ident + ': ' + text)
