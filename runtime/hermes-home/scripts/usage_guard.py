#!/usr/bin/env python3
"""Deterministic Codex-usage and Hermes-session guard.

Designed for a no-agent Hermes cron job. Prints nothing when healthy; stdout is
only an actionable alert. State is stored outside the Obsidian vault.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# Hermes cron launches .py scripts with a bare Python interpreter, without
# the gateway's activated dependency set. The weekly-usage checks below are
# stdlib-only; an unavailable optional YAML parser must not disable them.
try:
    import yaml
except ModuleNotFoundError:
    yaml = None

HERMES = "/home/hermes/.local/bin/hermes"
HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
STATE_PATH = HOME / "cache" / "usage_guard" / "state.json"
DB_PATH = HOME / "state.db"
CONFIG_PATH = HOME / "config.yaml"
REPO_PATH = HOME / "hermes-agent"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"samples": [], "alerts": {}, "usage_failures": 0}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def fetch_usage() -> dict:
    proc = subprocess.run(
        [HERMES, "usage", "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    payload = json.loads(proc.stdout)
    weekly = next((w for w in payload.get("windows", []) if w.get("label") == "Weekly"), None)
    if not weekly or weekly.get("used_percent") is None:
        raise RuntimeError("Kein Weekly-Fenster in der Usage-Antwort")
    return {
        "provider": payload.get("provider"),
        "plan": payload.get("plan"),
        "used": float(weekly["used_percent"]),
        "resets_at": weekly.get("resets_at"),
        "fetched_at": payload.get("fetched_at"),
    }


def latest_telegram_session() -> dict | None:
    if not DB_PATH.exists():
        return None
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            """
            SELECT id, session_key, chat_id, source, title, model, model_config,
                   billing_provider, billing_base_url,
                   message_count, tool_call_count, api_call_count,
                   input_tokens, output_tokens, cache_read_tokens,
                   reasoning_tokens, started_at, last_activity_at
              FROM sessions
             WHERE source = 'telegram'
             ORDER BY COALESCE(last_activity_at, started_at) DESC
             LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        counts = con.execute(
            """
            SELECT SUM(CASE WHEN _compressed_summary = 1 THEN 1 ELSE 0 END) AS summaries,
                   SUM(CASE WHEN compacted = 1 THEN 1 ELSE 0 END) AS compacted_messages,
                   SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_messages
              FROM messages WHERE session_id = ?
            """,
            (result["id"],),
        ).fetchone()
        result.update(dict(counts))
        routing = con.execute(
            "SELECT entry_json FROM gateway_routing WHERE session_key = ? ORDER BY updated_at DESC LIMIT 1",
            (result.get("session_key"),),
        ).fetchone()
        if routing:
            try:
                entry = json.loads(routing["entry_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                entry = {}
            result["last_prompt_tokens"] = int(entry.get("last_prompt_tokens") or 0)
        return result
    finally:
        con.close()


def sample_at_or_before(samples: list[dict], cutoff: dt.datetime) -> dict | None:
    eligible = []
    for sample in samples:
        stamp = parse_time(sample.get("at"))
        if stamp and stamp <= cutoff:
            eligible.append((stamp, sample))
    return max(eligible, default=(None, None), key=lambda item: item[0])[1]


def mark_once(state: dict, key: str) -> bool:
    if state["alerts"].get(key):
        return False
    state["alerts"][key] = now_utc().isoformat()
    return True


def load_config() -> dict:
    try:
        parsed = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def effective_route(session: dict, config: dict) -> tuple[str, str, str]:
    """Resolve the route this Telegram chat will actually use."""
    model = str(session.get("model") or "").strip()
    provider = str(session.get("billing_provider") or "").strip()
    base_url = str(session.get("billing_base_url") or "").strip()

    platform = ((config.get("platforms") or {}).get(str(session.get("source") or "")) or {})
    overrides = platform.get("channel_overrides") or {}
    override = overrides.get(str(session.get("chat_id") or "")) or {}
    if isinstance(override, dict):
        model = str(override.get("model") or model).strip()
        provider = str(override.get("provider") or provider).strip()

    default_model = config.get("model") or {}
    if isinstance(default_model, dict):
        model = model or str(default_model.get("default") or "").strip()
        provider = provider or str(default_model.get("provider") or "").strip()
    return model, provider, base_url


def session_context_metrics(session: dict) -> dict | None:
    """Use Hermes' own resolver and threshold math, based on real provider prompt tokens."""
    # Without the gateway's Python dependencies the exact Hermes threshold
    # cannot be resolved. Skip context advice; never fail the usage monitor.
    if yaml is None:
        return None
    prompt_tokens = int(session.get("last_prompt_tokens") or 0)
    if prompt_tokens <= 0:
        return None

    config = load_config()
    model, provider, base_url = effective_route(session, config)
    if not model:
        return None

    compression = config.get("compression") or {}
    threshold_percent = float(compression.get("threshold") or 0.50)
    threshold_cap = compression.get("threshold_tokens")
    model_thresholds = compression.get("model_thresholds") or {}

    if str(REPO_PATH) not in sys.path:
        sys.path.insert(0, str(REPO_PATH))
    from agent.context_compressor import ContextCompressor

    compressor = ContextCompressor(
        model,
        threshold_percent=threshold_percent,
        provider=provider,
        base_url=base_url,
        threshold_tokens_cap=threshold_cap,
        model_thresholds=model_thresholds,
    )
    threshold_tokens = int(compressor.threshold_tokens)
    context_length = int(compressor.context_length)
    ratio = prompt_tokens / threshold_tokens if threshold_tokens > 0 else 0.0
    return {
        "model": model,
        "provider": provider,
        "prompt_tokens": prompt_tokens,
        "context_length": context_length,
        "threshold_tokens": threshold_tokens,
        "threshold_ratio": ratio,
    }


def build_alerts(state: dict, usage: dict, session: dict | None) -> list[str]:
    alerts: list[str] = []
    used = usage["used"]
    reset_key = usage.get("resets_at") or "unknown-reset"

    previous_reset = state.get("reset_key")
    if previous_reset != reset_key:
        state["alerts"] = {}
        state["samples"] = []
        state["reset_key"] = reset_key

    for threshold, label in ((85, "kritisch"), (70, "hoch"), (50, "halb verbraucht")):
        if used >= threshold and mark_once(state, f"usage:{reset_key}:{threshold}"):
            alerts.append(f"Weekly Usage {used:.0f}% ({label}). Nicht notwendige Hintergrundarbeit pausieren.")
            break

    now = now_utc()
    old_6h = sample_at_or_before(state["samples"], now - dt.timedelta(hours=6))
    old_24h = sample_at_or_before(state["samples"], now - dt.timedelta(hours=24))
    if old_6h and used - float(old_6h["used"]) >= 4:
        delta = used - float(old_6h["used"])
        bucket = int(now.timestamp() // (6 * 3600))
        if mark_once(state, f"velocity6:{reset_key}:{bucket}"):
            alerts.append(f"Ungewöhnlich schneller Verbrauch: +{delta:.0f} Prozentpunkte in höchstens 6 Stunden.")
    elif old_24h and used - float(old_24h["used"]) >= 8:
        delta = used - float(old_24h["used"])
        bucket = int(now.timestamp() // (24 * 3600))
        if mark_once(state, f"velocity24:{reset_key}:{bucket}"):
            alerts.append(f"Hoher Tagesverbrauch: +{delta:.0f} Prozentpunkte in höchstens 24 Stunden.")

    if session:
        sid = session["id"]
        metrics = session_context_metrics(session)
        session["context_metrics"] = metrics
        if metrics:
            ratio = float(metrics["threshold_ratio"])
            level = 2 if ratio >= 0.85 else 1 if ratio >= 0.60 else 0
            levels = state.setdefault("context_levels", {})
            previous_level = int(levels.get(sid) or 0)

            # A real compaction can drop prompt usage sharply inside the same session.
            # Rearm only after it falls well below the first warning threshold.
            if ratio < 0.50:
                previous_level = 0
                levels.pop(sid, None)

            if level > previous_level:
                levels[sid] = level
                used_pct = ratio * 100
                prompt = int(metrics["prompt_tokens"])
                threshold = int(metrics["threshold_tokens"])
                model = metrics["model"]
                if level == 2:
                    alerts.append(
                        f"Session-Kontext bei {used_pct:.0f}% der echten Kompressionsschwelle "
                        f"({prompt:,}/{threshold:,} Prompt-Tokens, {model}). Aktuellen Block abschließen "
                        "und jetzt einen neuen Chat starten; Hermes komprimiert sonst sehr bald."
                    )
                else:
                    alerts.append(
                        f"Session-Kontext bei {used_pct:.0f}% der echten Kompressionsschwelle "
                        f"({prompt:,}/{threshold:,} Prompt-Tokens, {model}). Bei einem Themenwechsel "
                        "einen neuen Chat beginnen."
                    )

            # Keep only the active session's level so state cannot grow without bound.
            state["context_levels"] = {sid: int(levels.get(sid) or 0)} if levels.get(sid) else {}

    return alerts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    state = load_state()
    state.setdefault("samples", [])
    state.setdefault("alerts", {})

    try:
        usage = fetch_usage()
        state["usage_failures"] = 0
    except Exception as exc:
        state["usage_failures"] = int(state.get("usage_failures", 0)) + 1
        if not args.dry_run:
            save_state(state)
        if state["usage_failures"] == 3:
            print(f"Usage-Guard konnte das Weekly Limit dreimal nicht lesen: {exc}")
        return 0

    session = latest_telegram_session()
    # Hermes already compacts context automatically. Keep the session snapshot
    # for passive diagnostics, but do not send unsolicited /new advice.
    alerts = build_alerts(state, usage, None)
    stamp = now_utc().isoformat()
    state["samples"].append({"at": stamp, "used": usage["used"], "resets_at": usage.get("resets_at")})
    cutoff = now_utc() - dt.timedelta(days=8)
    state["samples"] = [s for s in state["samples"] if (parse_time(s.get("at")) or cutoff) >= cutoff]
    state["last_usage"] = usage
    state["last_session"] = session

    if not args.dry_run:
        save_state(state)
    if alerts:
        print("⚠️ Token-/Kontext-Guard\n" + "\n".join(f"• {line}" for line in alerts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
