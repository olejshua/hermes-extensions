from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

from .context_inbox import (
    _chmod_private_file,
    _chmod_sqlite_sidecars,
    _prepare_private_file_parent,
    _reject_symlink_sqlite_paths,
    atomic_text_writer,
    normalize_timestamp,
    now_iso,
    redact_sensitive_text,
    stable_json,
)

DEFAULT_MAIL_DB = Path("~/.hermes/second-brain/mail-context.sqlite3").expanduser()
DEFAULT_MAIL_OUTPUT_DIR = Path("~/.hermes/second-brain/import/mail").expanduser()
DEFAULT_ACCOUNTS = ("icloud", "gmail", "acme")
DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT = 120.0
DEFAULT_WORKERS = 4
DEFAULT_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 0.25
EXPORT_BODY_LIMIT = 8000

CATEGORIES = (
    "personal",
    "work",
    "education",
    "finance",
    "legal-admin",
    "housing",
    "travel",
    "health",
    "calendar",
    "shopping-shipping",
    "receipts",
    "subscriptions-newsletters",
    "security-auth",
    "social",
    "support",
    "sent",
    "drafts",
    "spam",
    "other",
)

CATEGORY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("security-auth", ("verification code", "one-time code", "2fa", "mfa", "login code", "auth code", "security alert", "password reset", "bestätigungscode", "sicherheitscode", "einmalcode", "anmeldecode", "passwort zurücksetzen", "passwort zuruecksetzen")),
    ("finance", ("invoice", "payment", "bank", "tax", "steuer", "rechnung", "zahlung", "finanzamt", "konto", "mahnung")),
    ("receipts", ("receipt", "your order", "beleg", "quittung", "bestellung", "kaufbeleg")),
    ("shopping-shipping", ("shipping", "delivery", "tracking", "shipment", "amazon", "paket", "lieferung", "versand", "sendung", "dhl", "ups")),
    ("health", ("doctor", "medical", "clinic", "health", "arzt", "praxis", "klinik", "gesundheit", "rezept")),
    ("support", ("support", "ticket", "case", "helpdesk", "hilfe", "kundenservice", "vorgang")),
    ("calendar", ("meeting", "appointment", "calendar", "invite", "termin", "einladung", "besprechung", "kalender")),
    ("travel", ("flight", "hotel", "booking", "train", "boarding", "reise", "flug", "bahn", "zug", "buchung", "ticket")),
    ("legal-admin", ("contract", "legal", "court", "insurance", "government", "vertrag", "anwalt", "gericht", "versicherung", "amt", "behörde", "behoerde")),
    ("housing", ("rent", "lease", "landlord", "utility", "wohnung", "miete", "vermieter", "nebenkosten", "strom", "gas")),
    ("education", ("course", "university", "school", "class", "lecture", "kurs", "uni", "schule", "vorlesung", "seminar")),
    ("subscriptions-newsletters", ("newsletter", "unsubscribe", "subscription", "digest", "abmelden", "abo", "rundbrief")),
    ("social", ("linkedin", "facebook", "instagram", "twitter", "x.com", "social", "netzwerk")),
    ("work", ("project", "meeting", "client", "invoice", "github", "linear", "slack", "arbeit", "kunde", "projekt", "rechnung")),
    ("personal", ("family", "friend", "dinner", "home", "familie", "freund", "essen", "privat")),
)


@dataclass
class MailRecord:
    account: str
    folder: str
    envelope_id: str
    fingerprint: str
    date: str
    sender_display: str
    sender_domain: str
    recipient_display: str
    recipient_domain: str
    subject: str
    flags: tuple[str, ...]
    direction: str
    has_attachment: bool
    category: str
    urgency: str
    action_hint: str
    reply_hint: str
    full_body: str
    sanitized_body: str
    error_state: str = ""

    @property
    def message_key(self) -> str:
        return stable_message_key(self.account, self.folder, self.envelope_id)

    @property
    def tags(self) -> tuple[str, ...]:
        tags = [self.category, f"urgency:{self.urgency}", f"action:{self.action_hint}", f"reply:{self.reply_hint}"]
        if self.has_attachment:
            tags.append("attachment")
        return tuple(tags)


@dataclass
class CollectSummary:
    accounts: int = 0
    folders: int = 0
    scanned: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    materialized: int = 0
    errors: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    failed_accounts: int = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "accounts": self.accounts,
            "folders": self.folders,
            "scanned": self.scanned,
            "new": self.new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "materialized": self.materialized,
            "errors": self.errors,
            "categories": dict(sorted(self.categories.items())),
        }


class MailContextVault:
    def __init__(self, db_path: Path | str = DEFAULT_MAIL_DB):
        self.db_path = Path(db_path).expanduser()
        prepare_db_path(self.db_path)
        _prepare_private_file_parent(self.db_path.parent)
        prepare_db_path(self.db_path)
        _reject_symlink_sqlite_paths(self.db_path)
        _chmod_private_file(self.db_path)
        _chmod_sqlite_sidecars(self.db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _chmod_private_file(self.db_path)
        _chmod_sqlite_sidecars(self.db_path)
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mail_messages(
                  message_key TEXT PRIMARY KEY,
                  account TEXT NOT NULL,
                  folder TEXT NOT NULL,
                  envelope_id TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,
                  message_date TEXT NOT NULL,
                  sender_display TEXT NOT NULL,
                  sender_domain TEXT NOT NULL,
                  recipient_display TEXT NOT NULL,
                  recipient_domain TEXT NOT NULL,
                  subject TEXT NOT NULL,
                  flags_json TEXT NOT NULL,
                  direction TEXT NOT NULL,
                  has_attachment INTEGER NOT NULL,
                  category TEXT NOT NULL,
                  urgency TEXT NOT NULL,
                  action_hint TEXT NOT NULL,
                  reply_hint TEXT NOT NULL,
                  full_body TEXT NOT NULL,
                  sanitized_body TEXT NOT NULL,
                  first_seen_at TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL,
                  materialized_at TEXT NOT NULL DEFAULT '',
                  error_state TEXT NOT NULL DEFAULT '',
                  UNIQUE(account, folder, envelope_id)
                );
                CREATE INDEX IF NOT EXISTS idx_mail_messages_date ON mail_messages(message_date);
                CREATE INDEX IF NOT EXISTS idx_mail_messages_category ON mail_messages(category);
                CREATE TABLE IF NOT EXISTS mail_message_failures(
                  message_key TEXT PRIMARY KEY,
                  account TEXT NOT NULL,
                  folder TEXT NOT NULL,
                  envelope_id TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,
                  stage TEXT NOT NULL,
                  error_kind TEXT NOT NULL,
                  attempts INTEGER NOT NULL,
                  first_failed_at TEXT NOT NULL,
                  last_failed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mail_message_failures_scope ON mail_message_failures(account, folder);
                """
            )

    def fingerprint_for(self, account: str, folder: str, envelope_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT fingerprint FROM mail_messages WHERE account=? AND folder=? AND envelope_id=?", (account, folder, envelope_id)).fetchone()
        return str(row["fingerprint"]) if row else None

    def upsert(self, record: MailRecord) -> str:
        stamp = now_iso()
        existing = self.fingerprint_for(record.account, record.folder, record.envelope_id)
        status = "new" if existing is None else "unchanged" if existing == record.fingerprint else "updated"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO mail_messages(message_key,account,folder,envelope_id,fingerprint,message_date,sender_display,sender_domain,recipient_display,recipient_domain,subject,flags_json,direction,has_attachment,category,urgency,action_hint,reply_hint,full_body,sanitized_body,first_seen_at,last_seen_at,error_state)
                VALUES(:message_key,:account,:folder,:envelope_id,:fingerprint,:date,:sender_display,:sender_domain,:recipient_display,:recipient_domain,:subject,:flags_json,:direction,:has_attachment,:category,:urgency,:action_hint,:reply_hint,:full_body,:sanitized_body,:first_seen_at,:last_seen_at,:error_state)
                ON CONFLICT(account,folder,envelope_id) DO UPDATE SET
                  fingerprint=excluded.fingerprint,
                  message_date=excluded.message_date,
                  sender_display=excluded.sender_display,
                  sender_domain=excluded.sender_domain,
                  recipient_display=excluded.recipient_display,
                  recipient_domain=excluded.recipient_domain,
                  subject=excluded.subject,
                  flags_json=excluded.flags_json,
                  direction=excluded.direction,
                  has_attachment=excluded.has_attachment,
                  category=excluded.category,
                  urgency=excluded.urgency,
                  action_hint=excluded.action_hint,
                  reply_hint=excluded.reply_hint,
                  full_body=excluded.full_body,
                  sanitized_body=excluded.sanitized_body,
                  last_seen_at=excluded.last_seen_at,
                  error_state=excluded.error_state
                """,
                {
                    **record.__dict__,
                    "message_key": record.message_key,
                    "flags_json": stable_json(list(record.flags)),
                    "has_attachment": 1 if record.has_attachment else 0,
                    "first_seen_at": stamp,
                    "last_seen_at": stamp,
                },
            )
        return status

    def mark_materialized(self, record: MailRecord) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE mail_messages SET materialized_at=? WHERE message_key=?", (now_iso(), record.message_key))

    def unresolved_failure_keys(self, account: str, folder: str) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT message_key FROM mail_message_failures WHERE account=? AND folder=?", (account, folder)).fetchall()
        return {str(row["message_key"]) for row in rows}

    def mark_failure(self, account: str, folder: str, envelope_id: str, fingerprint: str, stage: str, exc: BaseException) -> None:
        stamp = now_iso()
        message_key = stable_message_key(account, folder, envelope_id)
        error_kind = bounded_error_kind(exc)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO mail_message_failures(message_key,account,folder,envelope_id,fingerprint,stage,error_kind,attempts,first_failed_at,last_failed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(message_key) DO UPDATE SET
                  fingerprint=excluded.fingerprint,
                  stage=excluded.stage,
                  error_kind=excluded.error_kind,
                  attempts=mail_message_failures.attempts + 1,
                  last_failed_at=excluded.last_failed_at
                """,
                (message_key, account, folder, envelope_id, fingerprint, stage[:32], error_kind, 1, stamp, stamp),
            )

    def clear_failure(self, account: str, folder: str, envelope_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM mail_message_failures WHERE message_key=?", (stable_message_key(account, folder, envelope_id),))

    def prune_failures(self, account: str, folder: str, keep_keys: set[str]) -> int:
        failures = self.unresolved_failure_keys(account, folder)
        stale = failures - keep_keys
        if not stale:
            return 0
        with self.connect() as conn:
            conn.executemany("DELETE FROM mail_message_failures WHERE message_key=?", [(key,) for key in stale])
        return len(stale)


def collect_mail_context(
    accounts: list[str],
    folder_overrides: dict[str, list[str]],
    db_path: Path,
    output_dir: Path,
    himalaya: str,
    page_size: int = DEFAULT_PAGE_SIZE,
    full: bool = False,
    max_pages: int | None = None,
    max_messages: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
    retries: int = DEFAULT_RETRIES,
    retry_backoff: float = DEFAULT_RETRY_BACKOFF,
) -> tuple[CollectSummary, int]:
    vault = MailContextVault(db_path)
    safe_output = prepare_output_dir(output_dir)
    summary = CollectSummary(accounts=len(accounts))
    stop_all = False
    for account in accounts:
        if stop_all:
            break
        account_discovery_failed = False
        try:
            override = folder_overrides.get(account) or folder_overrides.get("*")
            folders = override if override is not None else select_folders(account, discover_folders(himalaya, account, timeout))
        except Exception:
            summary.errors += 1
            summary.failed_accounts += 1
            continue
        summary.folders += len(folders)
        failed_folders = 0
        completed_folders = 0
        for folder in folders:
            if stop_all:
                break
            page = 1
            folder_failed = False
            complete_traversal = False
            unresolved_at_start = vault.unresolved_failure_keys(account, folder)
            seen_failure_keys: set[str] = set()
            failed_this_run: set[str] = set()
            while True:
                if stop_all:
                    break
                if max_pages is not None and page > max_pages:
                    break
                try:
                    envelopes = list_envelopes(himalaya, account, folder, page, page_size, timeout)
                except PageComplete:
                    complete_traversal = True
                    break
                except Exception:
                    summary.errors += 1
                    folder_failed = True
                    break
                if not envelopes:
                    complete_traversal = True
                    break
                page_unchanged = 0
                read_jobs: list[tuple[dict[str, Any], str, str]] = []
                for envelope in envelopes:
                    if max_messages is not None and summary.scanned >= max_messages:
                        stop_all = True
                        break
                    envelope_id = envelope_id_from(envelope)
                    if not envelope_id:
                        summary.errors += 1
                        continue
                    env_fp = envelope_fingerprint(account, folder, envelope)
                    message_key = stable_message_key(account, folder, envelope_id)
                    if message_key in unresolved_at_start:
                        seen_failure_keys.add(message_key)
                    if not full and message_key not in unresolved_at_start and vault.fingerprint_for(account, folder, envelope_id) == env_fp:
                        summary.unchanged += 1
                        page_unchanged += 1
                        continue
                    read_jobs.append((envelope, envelope_id, env_fp))
                    summary.scanned += 1
                if read_jobs:
                    for envelope, envelope_id, env_fp, body, error in read_messages_bounded(himalaya, account, folder, read_jobs, timeout, workers, retries, retry_backoff):
                        if error is not None:
                            summary.errors += 1
                            vault.mark_failure(account, folder, envelope_id, env_fp, "read", error)
                            failed_this_run.add(stable_message_key(account, folder, envelope_id))
                            continue
                        try:
                            record = build_record(account, folder, envelope_id, envelope, body)
                            status = vault.upsert(record)
                            if materialize_record(record, safe_output):
                                summary.materialized += 1
                            vault.mark_materialized(record)
                            vault.clear_failure(account, folder, envelope_id)
                            setattr(summary, status, getattr(summary, status) + 1)
                            summary.categories[record.category] = summary.categories.get(record.category, 0) + 1
                        except Exception as exc:
                            summary.errors += 1
                            vault.mark_failure(account, folder, envelope_id, env_fp, "materialize", exc)
                            failed_this_run.add(stable_message_key(account, folder, envelope_id))
                if max_messages is not None and summary.scanned >= max_messages:
                    stop_all = True
                    break
                unresolved_remaining = bool(unresolved_at_start - seen_failure_keys)
                if not full and page_unchanged == len(envelopes) and not unresolved_remaining:
                    break
                if len(envelopes) < page_size:
                    complete_traversal = True
                    break
                page += 1
            if complete_traversal:
                vault.prune_failures(account, folder, seen_failure_keys | failed_this_run)
            if folder_failed:
                failed_folders += 1
            else:
                completed_folders += 1
        if account_discovery_failed or (folders and failed_folders == len(folders) and completed_folders == 0):
            summary.failed_accounts += 1
    return summary, 1 if summary.failed_accounts >= len(accounts) else 0


def discover_folders(himalaya: str, account: str, timeout: float) -> list[str]:
    data = run_himalaya_json([himalaya, "folder", "list", "-a", account, "-o", "json", "--quiet"], timeout)
    folders: list[str] = []
    for item in ensure_list(data):
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("folder") or item.get("path") or item.get("mailbox") or "")
        else:
            name = ""
        if name:
            folders.append(name)
    return folders


def select_folders(account: str, folders: list[str]) -> list[str]:
    if account.lower() != "gmail":
        return folders
    wanted = {"[gmail]/all mail", "[gmail]/spam", "[gmail]/trash", "[gmail]/drafts"}
    by_lower = {folder.lower(): folder for folder in folders}
    return [by_lower[key] for key in ("[gmail]/all mail", "[gmail]/spam", "[gmail]/trash", "[gmail]/drafts") if key in by_lower]


def list_envelopes(himalaya: str, account: str, folder: str, page: int, page_size: int, timeout: float) -> list[dict[str, Any]]:
    cmd = [himalaya, "envelope", "list", "-a", account, "-f", folder, "-p", str(page), "-s", str(page_size), "-o", "json", "--quiet"]
    try:
        data = run_himalaya_json(cmd, timeout)
    except subprocess.CalledProcessError as exc:
        if re.search(r"(?i)(out.?of.?range|no such page|invalid page|page.*range)", (exc.stderr or "") + (exc.stdout or "")):
            raise PageComplete() from exc
        raise
    return [item for item in ensure_list(data) if isinstance(item, dict)]


def read_message(himalaya: str, account: str, folder: str, envelope_id: str, timeout: float) -> str:
    data = run_himalaya_json([himalaya, "message", "read", "-a", account, "-f", folder, "--preview", "--no-headers", "-o", "json", "--quiet", envelope_id], timeout)
    if isinstance(data, dict):
        for key in ("body", "text", "content", "plain", "message"):
            if key in data:
                return str(data[key] or "")
    return str(data or "")


def read_message_with_retries(himalaya: str, account: str, folder: str, envelope_id: str, timeout: float, retries: int, retry_backoff: float) -> str:
    last_error: BaseException | None = None
    for attempt in range(max(1, retries)):
        try:
            return read_message(himalaya, account, folder, envelope_id, timeout)
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries and retry_backoff > 0:
                time.sleep(retry_backoff * (attempt + 1))
    if last_error is not None:
        raise last_error
    return ""


def read_messages_bounded(
    himalaya: str,
    account: str,
    folder: str,
    read_jobs: list[tuple[dict[str, Any], str, str]],
    timeout: float,
    workers: int,
    retries: int,
    retry_backoff: float,
) -> list[tuple[dict[str, Any], str, str, str, BaseException | None]]:
    if not read_jobs:
        return []
    max_workers = min(workers, len(read_jobs))
    results: dict[int, tuple[dict[str, Any], str, str, str, BaseException | None]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(read_message_with_retries, himalaya, account, folder, envelope_id, timeout, retries, retry_backoff): (index, envelope, envelope_id, env_fp)
            for index, (envelope, envelope_id, env_fp) in enumerate(read_jobs)
        }
        for future in concurrent.futures.as_completed(futures):
            index, envelope, envelope_id, env_fp = futures[future]
            try:
                results[index] = (envelope, envelope_id, env_fp, future.result(), None)
            except Exception as exc:
                results[index] = (envelope, envelope_id, env_fp, "", exc)
    return [results[index] for index in range(len(read_jobs))]


def run_himalaya_json(cmd: list[str], timeout: float) -> Any:
    result = subprocess.run(cmd, shell=False, text=True, capture_output=True, timeout=timeout, check=True)
    text = result.stdout.strip()
    if not text:
        return []
    return json.loads(text)


def ensure_list(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("folders", "envelopes", "messages", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def envelope_id_from(envelope: dict[str, Any]) -> str:
    return str(envelope.get("id") or envelope.get("uid") or envelope.get("message_id") or envelope.get("message-id") or "")


def build_record(account: str, folder: str, envelope_id: str, envelope: dict[str, Any], full_body: str) -> MailRecord:
    subject = collapse_ws(str(envelope.get("subject") or ""))
    flags = tuple(str(flag).lower() for flag in ensure_str_list(envelope.get("flags", [])))
    sender_display, sender_domain = address_public_parts(first_value(envelope, "from", "sender"))
    recipient_display, recipient_domain = address_public_parts(first_value(envelope, "to", "recipients"))
    date = safe_mail_date(first_value(envelope, "date", "received_at", "internal_date"))
    has_attachment = detect_attachment(envelope)
    direction = infer_direction(folder, flags)
    category = classify_mail(account, folder, sender_domain, subject, full_body, flags, has_attachment)
    urgency = classify_urgency(subject, full_body)
    action_hint = classify_action(subject, full_body)
    reply_hint = classify_reply(direction, subject, full_body)
    sanitized = "" if category == "security-auth" else redact_sensitive_text(full_body)
    fingerprint = message_fingerprint(account, folder, envelope, full_body)
    return MailRecord(
        account=account,
        folder=folder,
        envelope_id=envelope_id,
        fingerprint=fingerprint,
        date=date,
        sender_display=sender_display,
        sender_domain=sender_domain,
        recipient_display=recipient_display,
        recipient_domain=recipient_domain,
        subject=redact_sensitive_text(subject),
        flags=flags,
        direction=direction,
        has_attachment=has_attachment,
        category=category,
        urgency=urgency,
        action_hint=action_hint,
        reply_hint=reply_hint,
        full_body=full_body,
        sanitized_body=sanitized,
    )


def classify_mail(account: str, folder: str, sender_domain: str, subject: str, body: str, flags: tuple[str, ...], has_attachment: bool) -> str:
    folder_l = folder.lower()
    text = f"{sender_domain} {subject} {body}".lower()
    if "draft" in folder_l:
        return "drafts"
    if "spam" in folder_l or "junk" in folder_l:
        return "spam"
    if "sent" in folder_l or "\\sent" in flags:
        return "sent"
    for category, signals in CATEGORY_PATTERNS:
        if any(signal_matches(text, signal) for signal in signals):
            return category
    if has_attachment and re.search(r"\b(pdf|vertrag|contract|rechnung|invoice)\b", text):
        return "legal-admin"
    return "other"


def signal_matches(text: str, signal: str) -> bool:
    if re.fullmatch(r"[\w äöüß-]+", signal, flags=re.IGNORECASE):
        return re.search(rf"(?<!\w){re.escape(signal)}(?!\w)", text, flags=re.IGNORECASE) is not None
    return signal.lower() in text


def classify_urgency(subject: str, body: str) -> str:
    text = f"{subject} {body}".lower()
    if re.search(r"\b(urgent|asap|immediately|today|deadline|dringend|sofort|heute|frist)\b", text):
        return "high"
    if re.search(r"\b(soon|tomorrow|reminder|morgen|erinnerung|bitte)\b", text):
        return "medium"
    return "low"


def classify_action(subject: str, body: str) -> str:
    text = f"{subject} {body}".lower()
    if re.search(r"\b(pay|sign|approve|review|confirm|book|send|bezahlen|unterschreiben|bestaetigen|bestätigen|prüfen|pruefen|senden|buchen)\b", text):
        return "action"
    return "reference"


def classify_reply(direction: str, subject: str, body: str) -> str:
    text = f"{subject} {body}".lower()
    if direction == "inbound" and re.search(r"\?|reply|respond|antwort|rückmeldung|rueckmeldung", text):
        return "reply-needed"
    return "no-reply"


def infer_direction(folder: str, flags: tuple[str, ...]) -> str:
    lower = folder.lower()
    if "sent" in lower or "\\sent" in flags:
        return "outbound"
    if "draft" in lower:
        return "draft"
    return "inbound"


def materialize_record(record: MailRecord, output_dir: Path) -> bool:
    filename = "mail-" + hashlib.sha256(record.message_key.encode("utf-8")).hexdigest()[:32] + ".md"
    target = output_dir / filename
    if target.is_symlink():
        raise ValueError(f"refusing to overwrite symlink: {target}")
    content = record_markdown(record)
    if target.exists() and target.read_text(encoding="utf-8") == content:
        return False
    with atomic_text_writer(target) as fh:
        fh.write(content)
    return True


def record_markdown(record: MailRecord) -> str:
    exported_subject = "[security-auth subject omitted]" if record.category == "security-auth" else record.subject
    metadata = {
        "source": "mail-context",
        "mailbox_class": mailbox_class(record.account),
        "folder_class": folder_class(record.folder),
        "date": record.date,
        "from": {"display": record.sender_display, "domain": record.sender_domain},
        "to": {"display": record.recipient_display, "domain": record.recipient_domain},
        "subject": exported_subject,
        "direction": record.direction,
        "has_attachment": record.has_attachment,
        "category": record.category,
        "tags": list(record.tags),
    }
    lines = ["---"]
    for key, value in metadata.items():
        lines.append(f"{key}: {json.dumps(value, sort_keys=True)}")
    lines.append("---")
    lines.append("")
    if record.category == "security-auth":
        lines.append("[Body omitted for security-auth mail.]")
    else:
        lines.append(collapse_ws(record.sanitized_body)[:EXPORT_BODY_LIMIT])
    lines.append("")
    return "\n".join(lines)


def prepare_output_dir(output_dir: Path) -> Path:
    output_dir = output_dir.expanduser()
    reject_symlink_ancestors(output_dir)
    if output_dir.exists() and output_dir.is_symlink():
        raise ValueError(f"output directory must not be a symlink: {output_dir}")
    _prepare_private_file_parent(output_dir)
    reject_symlink_ancestors(output_dir)
    if not output_dir.is_dir():
        raise ValueError(f"output path is not a directory: {output_dir}")
    return output_dir.resolve()


def prepare_db_path(db_path: Path) -> None:
    reject_symlink_ancestors(db_path)


def reject_symlink_ancestors(path: Path) -> None:
    path = path.expanduser()
    allowed_system_aliases = {Path("/tmp"), Path("/var")}
    if not path.is_absolute():
        path = Path(os.path.abspath(Path.cwd() / path))
    current = Path(path.anchor or "/")
    parts = path.parts[1:]
    for part in parts[:-1]:
        current = current / part
        if current in allowed_system_aliases:
            continue
        try:
            if current.is_symlink():
                raise ValueError(f"refusing symlinked path component: {current}")
        except OSError as exc:
            raise ValueError(f"cannot validate path component: {current}") from exc


def mailbox_class(account: str) -> str:
    lower = account.lower()
    configured_defaults = {
        "icloud": "personal",
        "gmail": "work",
        "acme": "startup",
    }
    if lower in configured_defaults:
        return configured_defaults[lower]
    if any(token in lower for token in ("work", "company", "corp")):
        return "work"
    if "startup" in lower:
        return "startup"
    if any(token in lower for token in ("icloud", "gmail", "personal", "private")):
        return "personal"
    return "other"


def folder_class(folder: str) -> str:
    lower = folder.lower()
    if "inbox" in lower:
        return "inbox"
    if "sent" in lower:
        return "sent"
    if "draft" in lower:
        return "drafts"
    if "spam" in lower or "junk" in lower:
        return "spam"
    if "trash" in lower or "bin" in lower or "deleted" in lower:
        return "trash"
    if "archive" in lower or "all mail" in lower:
        return "archive"
    return "custom"


def stable_message_key(account: str, folder: str, envelope_id: str) -> str:
    return "mail_" + hashlib.sha256(stable_json([account, folder, envelope_id]).encode("utf-8")).hexdigest()[:32]


def message_fingerprint(account: str, folder: str, envelope: dict[str, Any], full_body: str) -> str:
    del full_body
    return envelope_fingerprint(account, folder, envelope)


def envelope_fingerprint(account: str, folder: str, envelope: dict[str, Any]) -> str:
    return hashlib.sha256(stable_json({"account": account, "folder": folder, "envelope": public_envelope_payload(envelope)}).encode("utf-8")).hexdigest()


def public_envelope_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": envelope_id_from(envelope),
        "date": str(first_value(envelope, "date", "received_at", "internal_date")),
        "from": domain_only(first_value(envelope, "from", "sender")),
        "to": domain_only(first_value(envelope, "to", "recipients")),
        "subject": str(envelope.get("subject") or ""),
        "flags": ensure_str_list(envelope.get("flags", [])),
        "has_attachment": detect_attachment(envelope),
    }


def address_public_parts(value: Any) -> tuple[str, str]:
    entries = address_entries(value)
    display = next((collapse_ws(name) for name, _email in entries if collapse_ws(name)), "")
    domain = next((domain_from_email(email) for _name, email in entries if domain_from_email(email)), "")
    return display[:120], domain


def address_entries(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, list) or isinstance(value, tuple):
        entries: list[tuple[str, str]] = []
        for item in value:
            entries.extend(address_entries(item))
        return entries
    if isinstance(value, dict):
        name = str(value.get("name") or value.get("display") or "").strip()
        email = str(value.get("addr") or value.get("address") or value.get("email") or "").strip()
        return [(name, email)] if name or email else []
    raw = str(value or "").strip()
    if not raw:
        return []
    parsed = getaddresses([raw])
    if parsed:
        return [(name, email) for name, email in parsed if name or email]
    name, email = parseaddr(raw)
    return [(name, email or raw)] if name or email or raw else []


def domain_only(value: Any) -> str:
    return address_public_parts(value)[1]


def domain_from_email(value: str) -> str:
    match = re.search(r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", value)
    return match.group(1).lower() if match else ""


def safe_mail_date(value: Any) -> str:
    normalized = normalize_timestamp(value)
    if normalized:
        return normalized
    text = str(value or "").strip()
    if text:
        try:
            parsed = parsedate_to_datetime(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError):
            pass
    return "1970-01-01T00:00:00Z"


def detect_attachment(envelope: dict[str, Any]) -> bool:
    for key in ("has_attachment", "hasAttachments", "attachments"):
        value = envelope.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, list):
            return bool(value)
        if isinstance(value, int):
            return bool(value)
    flags = " ".join(ensure_str_list(envelope.get("flags", []))).lower()
    return "attachment" in flags


def first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return ""


def ensure_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [part for item in value for part in ensure_str_list(item)]
    if isinstance(value, tuple):
        return [part for item in value for part in ensure_str_list(item)]
    if isinstance(value, dict):
        return [str(value.get("addr") or value.get("address") or value.get("email") or value.get("name") or "")]
    return [str(value)] if value not in (None, "") else []


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def bounded_error_kind(exc: BaseException) -> str:
    name = type(exc).__name__
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80] or "Error"


class PageComplete(Exception):
    pass


def accounts_from_args(values: list[str] | None, env: str | None) -> list[str]:
    raw: list[str] = []
    if values:
        for value in values:
            raw.extend(part.strip() for part in value.split(","))
    elif env:
        raw.extend(part.strip() for part in env.split(","))
    else:
        raw.extend(DEFAULT_ACCOUNTS)
    return [item for item in dict.fromkeys(raw) if item]


def folder_overrides_from_args(values: list[str] | None) -> dict[str, list[str]]:
    overrides: dict[str, list[str]] = {}
    for value in values or []:
        account = "*"
        folder = value
        if ":" in value:
            account, folder = value.split(":", 1)
            account = account.strip() or "*"
        overrides.setdefault(account, []).extend(part.strip() for part in folder.split(",") if part.strip())
    return overrides


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mail-context-collector")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--account", action="append")
    parser.add_argument("--folder", action="append", help="folder override, optionally ACCOUNT:FOLDER")
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("MAIL_CONTEXT_DB", str(DEFAULT_MAIL_DB))))
    parser.add_argument("--output-dir", type=Path, default=Path(os.environ.get("MAIL_CONTEXT_OUTPUT_DIR", str(DEFAULT_MAIL_OUTPUT_DIR))))
    parser.add_argument("--himalaya", default=os.environ.get("HIMALAYA", "himalaya"))
    parser.add_argument("--page-size", type=int, default=int(os.environ.get("MAIL_CONTEXT_PAGE_SIZE", str(DEFAULT_PAGE_SIZE))))
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("MAIL_CONTEXT_TIMEOUT", str(DEFAULT_TIMEOUT))))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("MAIL_CONTEXT_WORKERS", str(DEFAULT_WORKERS))))
    parser.add_argument("--retries", type=int, default=int(os.environ.get("MAIL_CONTEXT_RETRIES", str(DEFAULT_RETRIES))))
    parser.add_argument("--retry-backoff", type=float, default=float(os.environ.get("MAIL_CONTEXT_RETRY_BACKOFF", str(DEFAULT_RETRY_BACKOFF))))
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.page_size <= 0:
        parser.error("--page-size must be positive")
    if args.max_pages is not None and args.max_pages <= 0:
        parser.error("--max-pages must be positive")
    if args.max_messages is not None and args.max_messages <= 0:
        parser.error("--max-messages must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.workers <= 0:
        parser.error("--workers must be positive")
    if args.retries <= 0:
        parser.error("--retries must be positive")
    if args.retry_backoff < 0:
        parser.error("--retry-backoff must be nonnegative")
    accounts = accounts_from_args(args.account, os.environ.get("MAIL_CONTEXT_ACCOUNTS"))
    if not accounts:
        parser.error("at least one account is required")
    try:
        summary, code = collect_mail_context(
            accounts=accounts,
            folder_overrides=folder_overrides_from_args(args.folder),
            db_path=args.db,
            output_dir=args.output_dir,
            himalaya=args.himalaya,
            page_size=args.page_size,
            full=args.full,
            max_pages=args.max_pages,
            max_messages=args.max_messages,
            timeout=args.timeout,
            workers=args.workers,
            retries=args.retries,
            retry_backoff=args.retry_backoff,
        )
    except Exception as exc:
        print(f"mail-context-collector: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary.public_dict(), sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
