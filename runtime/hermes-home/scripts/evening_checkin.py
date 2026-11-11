#!/usr/bin/env python3
from datetime import datetime
from zoneinfo import ZoneInfo

now = datetime.now(ZoneInfo("Europe/Berlin"))

# One-time exception requested on 2026-09-22: skip the normal 23:00 run.
# A separate one-shot check-in runs on 2026-09-23 at 01:00.
if now.date().isoformat() == "2026-09-22" and now.hour == 23:
    raise SystemExit(0)

stamp = now.strftime("%A, %d.%m.%Y")
print(f"**Abend-Check-in — {stamp}**")
print()
print("1. Konkretes Stealth-Ergebnis?")
print("2. Konkretes Acribic-Ergebnis?")
print("3. Meditation: ja/nein?")
print("4. Training oder Ruhetag? Ernährung vollständig erfasst: ja/nein?")
print("5. Was hast du vermieden – und was ist morgen der erste konkrete Schritt?")
