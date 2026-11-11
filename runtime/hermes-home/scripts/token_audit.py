#!/usr/bin/env python3
"""Read-only Hermes token audit for a one-shot morning check."""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path.home() / ".hermes/state.db"
BASE = Path.home() / ".hermes/usage-audits/2026-09-23-sol-telegram-baseline.json"
FIELDS = ("api_call_count", "input_tokens", "cache_read_tokens", "output_tokens")


def snapshot():
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT id, api_call_count, input_tokens, cache_read_tokens, output_tokens "
            "FROM sessions WHERE model = 'gpt-6-sol' AND source = 'telegram'"
        )
        data = {row[0]: dict(zip(FIELDS, row[1:])) for row in rows}
    return {"timestamp": datetime.now().astimezone().isoformat(), "sessions": data}


def totals(snap):
    return {k: sum(row[k] or 0 for row in snap["sessions"].values()) for k in FIELDS}


def main():
    current = snapshot()
    if len(sys.argv) > 1 and sys.argv[1] == "--baseline":
        BASE.parent.mkdir(parents=True, exist_ok=True)
        if BASE.exists():
            print("Baseline already exists; no overwrite.")
            return
        BASE.write_text(json.dumps(current, indent=2))
        t = totals(current)
        print(f"Baseline {current['timestamp']}: {t['api_call_count']} calls, "
              f"{t['cache_read_tokens']:,} cached input tokens, "
              f"{t['input_tokens']:,} uncached input tokens.")
        return
    before = json.loads(BASE.read_text())
    old, new = totals(before), totals(current)
    delta = {k: new[k] - old[k] for k in FIELDS}
    report = (f"Sol/Telegram-Token-Check seit {before['timestamp']}: "
              f"{delta['api_call_count']} Modellaufrufe, "
              f"{delta['cache_read_tokens']:,} Cache-Read-, "
              f"{delta['input_tokens']:,} uncached Input- und "
              f"{delta['output_tokens']:,} Output-Tokens.")
    if delta["api_call_count"]:
        avg = (delta["cache_read_tokens"] + delta["input_tokens"]) / delta["api_call_count"]
        report += f" Mittlere Inputgröße: {avg:,.0f} Tokens/Aufruf."
    else:
        report += " Ohne neue Modellaufrufe ist noch kein Vorher-Nachher-Vergleich möglich."
    print(report)


if __name__ == "__main__":
    main()
