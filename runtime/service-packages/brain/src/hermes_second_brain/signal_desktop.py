"""Secure, observation-only ingestion of macOS Signal Desktop history.

The collector reads Signal's live SQLCipher database in read-only mode.  The
Safe Storage password and database key remain in process memory, and the key is
sent to ``sqlcipher`` over stdin rather than argv or the environment.  Only
canonical Context Inbox events and whitelisted attachment metadata are stored.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .context_inbox import (
    DEFAULT_CONTEXT_DB,
    ContextInbox,
    _chmod_private_file,
    _prepare_private_file_parent,
    _safe_attachment_file,
    _signal_direction,
    atomic_text_writer,
)


SIGNAL_DIR = Path("~/Library/Application Support/Signal").expanduser()
DEFAULT_CONFIG_PATH = SIGNAL_DIR / "config.json"
DEFAULT_SIGNAL_DB = SIGNAL_DIR / "sql" / "db.sqlite"
DEFAULT_ATTACHMENTS_ROOT = SIGNAL_DIR / "attachments.noindex"
DEFAULT_STATE_PATH = Path("~/.hermes/second-brain/signal-desktop-history-state.json").expanduser()
DEFAULT_SQLCIPHER = "/opt/homebrew/bin/sqlcipher"

SAFE_STORAGE_SERVICE = "Signal Safe Storage"
SAFE_STORAGE_ACCOUNT = "Signal"
SIGNAL_DESKTOP_ACCOUNT = "signal-desktop"
IMPORT_STATUS_SOURCE = "signal-desktop-history"

MAX_CONFIG_BYTES = 1024 * 1024
MAX_STATE_BYTES = 64 * 1024
MAX_SQL_INPUT_BYTES = 1024 * 1024
MAX_QUERY_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_QUERY_ROW_BYTES = 8 * 1024 * 1024
MAX_QUERY_ROWS = 50_000
MAX_BATCH_ROWS = 500
DEFAULT_BATCH_ROWS = 250
DEFAULT_LOOKBACK_ROWS = 1000
DEFAULT_FULL_RESCAN_DAYS = 7.0
MAX_CONVERSATIONS = 50_000
MAX_ATTACHMENTS_PER_MESSAGE = 64
MAX_EDIT_VERSIONS = 100

_SECURITY = "/usr/bin/security"
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SALT = b"saltysalt"
_ITERATIONS = 1003
_IV = b" " * 16


class SignalDesktopError(Exception):
    """Base class whose messages are safe to expose in aggregate-only logs."""


class DependencyError(SignalDesktopError):
    """A required local executable or Python package is unavailable."""


class KeychainError(SignalDesktopError):
    """The Signal Safe Storage password could not be read."""


class DecryptionError(SignalDesktopError):
    """The wrapped database key could not be decrypted or validated."""


class SchemaError(SignalDesktopError):
    """The encrypted database schema is unsupported or unsafe to query."""


class CollectorBusyError(SignalDesktopError):
    """Another collector instance owns the private state lock."""


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class SchemaInfo:
    message_columns: tuple[str, ...]
    conversation_columns: tuple[str, ...]
    attachment_columns: tuple[str, ...]
    order_column: str
    signature: str


@dataclass(frozen=True)
class IngestResult:
    imported: int
    skipped: int
    scanned: int
    cursor: dict[str, int] | None
    resume_cursor: dict[str, int] | None
    order_col: str
    schema_signature: str


@dataclass
class PreparedSignalReader:
    reader: "SqlcipherProcessReader"
    schema: SchemaInfo
    attachments_root: Path | None

    def clear(self) -> None:
        self.reader.clear_key()


def _read_bounded_regular_file(path: Path | str, maximum: int, *, missing_ok: bool = False) -> bytes | None:
    candidate = Path(path).expanduser()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(candidate, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise SignalDesktopError("required local Signal file is unavailable") from None
    except OSError:
        raise SignalDesktopError("required local Signal file is unsafe or unreadable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise SignalDesktopError("required local Signal file has an unsafe shape")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise SignalDesktopError("required local Signal file exceeds its safety limit")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_signal_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    raw = _read_bounded_regular_file(path, MAX_CONFIG_BYTES)
    assert raw is not None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DecryptionError("Signal config is not valid JSON") from None
    if not isinstance(value, dict):
        raise DecryptionError("Signal config has an unexpected shape")
    return value


def read_safe_storage_password(
    *,
    service: str = SAFE_STORAGE_SERVICE,
    account: str = SAFE_STORAGE_ACCOUNT,
    keychain: str | Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = 30.0,
) -> bytearray:
    """Read Signal's Safe Storage password without echoing or logging it."""

    if not service or not account:
        raise KeychainError("Safe Storage Keychain selectors are invalid")
    argv = [_SECURITY, "find-generic-password", "-w", "-s", service, "-a", account]
    if keychain is not None:
        argv.append(str(Path(keychain).expanduser()))
    try:
        result = runner(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise KeychainError("Safe Storage Keychain lookup failed") from None
    if getattr(result, "returncode", 1) != 0:
        raise KeychainError("Safe Storage password is unavailable from the selected Keychain")
    password = getattr(result, "stdout", b"") or b""
    if isinstance(password, str):
        password = password.encode("utf-8")
    if password.endswith(b"\r\n"):
        password = password[:-2]
    elif password.endswith(b"\n"):
        password = password[:-1]
    if not password or len(password) > 4096:
        raise KeychainError("Safe Storage password has an unexpected size")
    return bytearray(password)


def _zeroize(value: bytearray | None) -> None:
    if value is not None:
        for index in range(len(value)):
            value[index] = 0


def _decode_encrypted_key(value: str) -> bytes:
    text = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]+", text) and len(text) % 2 == 0:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise DecryptionError("encryptedKey is not valid hex or base64") from None


def _aes128_cbc_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise DependencyError("the Signal collector requires the cryptography package") from None
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(_IV)).decryptor()
        return decryptor.update(ciphertext) + decryptor.finalize()
    except ValueError:
        raise DecryptionError("encryptedKey ciphertext is malformed") from None


def _pkcs7_unpad(value: bytes) -> bytes:
    if not value or len(value) % 16:
        raise DecryptionError("encryptedKey padding is invalid")
    count = value[-1]
    if count < 1 or count > 16 or value[-count:] != bytes([count]) * count:
        raise DecryptionError("encryptedKey padding is invalid")
    return value[:-count]


def derive_sqlcipher_key(config: dict[str, Any], password: bytes | bytearray) -> str:
    """Unwrap and validate Signal's 32-byte SQLCipher key in memory."""

    legacy = config.get("key") if isinstance(config, dict) else None
    if isinstance(legacy, str) and _HEX64_RE.fullmatch(legacy.strip()):
        return legacy.strip().lower()
    encrypted = config.get("encryptedKey") if isinstance(config, dict) else None
    if not isinstance(encrypted, str) or not encrypted.strip():
        raise DecryptionError("Signal config has no usable database key")
    blob = _decode_encrypted_key(encrypted)
    if len(blob) < 19 or blob[:3] != b"v10":
        raise DecryptionError("Signal encryptedKey is not the supported macOS v10 format")
    ciphertext = blob[3:]
    if len(ciphertext) % 16:
        raise DecryptionError("encryptedKey ciphertext is malformed")
    wrapping_key = hashlib.pbkdf2_hmac("sha1", bytes(password), _SALT, _ITERATIONS, 16)
    try:
        plaintext = _pkcs7_unpad(_aes128_cbc_decrypt(wrapping_key, ciphertext))
        key_text = plaintext.decode("ascii")
    except (UnicodeDecodeError, DecryptionError):
        raise DecryptionError("Signal database key decryption failed") from None
    if not _HEX64_RE.fullmatch(key_text):
        raise DecryptionError("decrypted Signal database key has an unexpected shape")
    return key_text.lower()


def _run_bounded_process(
    argv: Sequence[str],
    script: str,
    *,
    timeout: float,
    maximum_stdout: int = MAX_QUERY_OUTPUT_BYTES,
    maximum_stderr: int = 64 * 1024,
) -> _ProcessResult:
    """Run sqlcipher while bounding captured plaintext and diagnostic output."""

    encoded = script.encode("utf-8")
    if len(encoded) > MAX_SQL_INPUT_BYTES:
        raise SchemaError("generated SQL exceeds its safety limit")
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except OSError:
        raise DependencyError("sqlcipher could not be started") from None
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        streams = {process.stdout: (bytearray(), maximum_stdout), process.stderr: (bytearray(), maximum_stderr)}
        deadline = time.monotonic() + timeout
        input_offset = 0
        with selectors.DefaultSelector() as selector:
            for stream in streams:
                selector.register(stream, selectors.EVENT_READ)
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise SignalDesktopError("sqlcipher query timed out")
                for selected, _ in selector.select(min(remaining, 0.5)):
                    stream = selected.fileobj
                    if stream is process.stdin:
                        try:
                            written = os.write(
                                stream.fileno(), encoded[input_offset:input_offset + 64 * 1024]
                            )
                        except BlockingIOError:
                            continue
                        except (BrokenPipeError, OSError):
                            process.kill()
                            process.wait()
                            raise SignalDesktopError(
                                "sqlcipher terminated before accepting its query"
                            ) from None
                        input_offset += written
                        if input_offset >= len(encoded):
                            selector.unregister(stream)
                            stream.close()
                        continue
                    chunk = os.read(stream.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    target, maximum = streams[stream]
                    if len(target) + len(chunk) > maximum:
                        process.kill()
                        process.wait()
                        raise SchemaError("sqlcipher output exceeded its safety limit")
                    target.extend(chunk)
        try:
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise SignalDesktopError("sqlcipher query timed out") from None
        try:
            stdout = bytes(streams[process.stdout][0]).decode("utf-8")
            stderr = bytes(streams[process.stderr][0]).decode("utf-8", "replace")
        except UnicodeDecodeError:
            raise SchemaError("sqlcipher returned non-UTF-8 data") from None
        return _ProcessResult(returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _normalize_process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise SchemaError("sqlcipher returned non-UTF-8 data") from None
    return str(value)


class SqlcipherProcessReader:
    """Bounded JSON-row queries over a live SQLCipher database."""

    def __init__(
        self,
        db_path: str | Path,
        key_hex: str,
        *,
        sqlcipher_path: str | None = None,
        runner: Callable[..., Any] | None = None,
        timeout: float = 120.0,
        maximum_output_bytes: int = MAX_QUERY_OUTPUT_BYTES,
    ) -> None:
        if not _HEX64_RE.fullmatch(key_hex):
            raise DecryptionError("SQLCipher key has an unexpected shape")
        candidate = Path(db_path).expanduser()
        if candidate.is_symlink() or not candidate.is_file():
            raise SignalDesktopError("Signal database is unavailable or unsafe")
        for suffix in ("-wal", "-shm"):
            if Path(str(candidate) + suffix).is_symlink():
                raise SignalDesktopError("Signal database sidecar is unsafe")
        executable = sqlcipher_path or shutil.which("sqlcipher") or DEFAULT_SQLCIPHER
        resolved_executable = shutil.which(executable) if os.sep not in executable else executable
        if (
            not resolved_executable
            or not os.path.isfile(resolved_executable)
            or not os.access(resolved_executable, os.X_OK)
        ):
            raise DependencyError("sqlcipher executable is unavailable")
        try:
            resolved_database = candidate.resolve(strict=True)
        except OSError:
            raise SignalDesktopError("Signal database is unavailable or unsafe") from None
        self._db_path = str(resolved_database)
        self._key_hex = key_hex.lower()
        self._sqlcipher = resolved_executable
        self._runner = runner
        self._timeout = timeout
        self._maximum_output_bytes = maximum_output_bytes

    def clear_key(self) -> None:
        self._key_hex = ""

    def query_json(self, sql: str) -> list[dict[str, Any]]:
        if not self._key_hex:
            raise DecryptionError("SQLCipher reader key is no longer available")
        if "\x00" in sql or len(sql.encode("utf-8")) > MAX_SQL_INPUT_BYTES // 2:
            raise SchemaError("generated SQL is unsafe")
        script = (
            f"PRAGMA key = \"x'{self._key_hex}'\";\n"
            "PRAGMA cipher_compatibility = 4;\n"
            "PRAGMA query_only = ON;\n"
            "PRAGMA trusted_schema = OFF;\n"
            "BEGIN;\n"
            f"{sql.rstrip().rstrip(';')};\n"
            "COMMIT;\n"
        )
        argv = [
            self._sqlcipher,
            "-batch",
            "-bail",
            "-noheader",
            "-list",
            "-noinit",
            "-readonly",
            "-nofollow",
            "--",
            self._db_path,
        ]
        if self._runner is None:
            result = _run_bounded_process(
                argv,
                script,
                timeout=self._timeout,
                maximum_stdout=self._maximum_output_bytes,
            )
        else:
            try:
                result = self._runner(
                    argv,
                    input=script,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                )
            except (OSError, subprocess.SubprocessError):
                raise SignalDesktopError("sqlcipher query failed") from None
        stdout = _normalize_process_text(getattr(result, "stdout", ""))
        stderr = _normalize_process_text(getattr(result, "stderr", ""))
        if len(stdout.encode("utf-8")) > self._maximum_output_bytes:
            raise SchemaError("sqlcipher output exceeded its safety limit")
        if getattr(result, "returncode", 1) != 0 or stderr.strip():
            diagnostic = stderr.lower()
            if any(token in diagnostic for token in ("not a database", "file is encrypted", "hmac", "cipher")):
                raise DecryptionError("sqlcipher rejected the derived database key")
            raise SchemaError("sqlcipher could not execute a read-only schema query")
        rows: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            encoded_line = line.encode("utf-8")
            if len(encoded_line) > MAX_QUERY_ROW_BYTES:
                raise SchemaError("sqlcipher row exceeded its safety limit")
            if not line.strip():
                continue
            # SQLCipher emits a literal acknowledgement for PRAGMA key.  It is
            # control output, not a query row; every collector SELECT is a
            # json_object and therefore cannot be confused with this token.
            if line == "ok":
                continue
            if len(rows) >= MAX_QUERY_ROWS:
                raise SchemaError("sqlcipher row count exceeded its safety limit")
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                raise SchemaError("sqlcipher returned a non-JSON row") from None
            if not isinstance(value, dict):
                raise SchemaError("sqlcipher returned an unexpected JSON shape")
            rows.append(value)
        return rows


class Sqlite3Reader:
    """The same JSON-row interface for synthetic plaintext test fixtures."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def query_json(self, sql: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for raw in self._connection.execute(sql):
            value = json.loads(raw[0])
            if not isinstance(value, dict):
                raise SchemaError("fixture reader returned an unexpected JSON shape")
            rows.append(value)
            if len(rows) > MAX_QUERY_ROWS:
                raise SchemaError("fixture row count exceeded its safety limit")
        return rows


def validate_reader(reader: Any) -> None:
    rows = reader.query_json(
        "SELECT json_object('table_count', count(*)) FROM sqlite_master WHERE type = 'table'"
    )
    if len(rows) != 1 or not isinstance(rows[0].get("table_count"), int):
        raise DecryptionError("SQLCipher key validation returned an unexpected result")


def _identifier(name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(name):
        raise SchemaError("Signal schema contains an unsafe identifier")
    return name


def _tables(reader: Any) -> set[str]:
    rows = reader.query_json(
        "SELECT json_object('name', name) FROM sqlite_master "
        "WHERE type = 'table' ORDER BY name LIMIT 1001"
    )
    if len(rows) > 1000:
        raise SchemaError("Signal schema contains too many tables")
    return {str(row.get("name") or "") for row in rows}


def _columns(reader: Any, table: str) -> tuple[str, ...]:
    safe_table = _identifier(table)
    rows = reader.query_json(
        f"SELECT json_object('name', name) FROM pragma_table_info('{safe_table}') "
        "ORDER BY cid LIMIT 1001"
    )
    if len(rows) > 1000:
        raise SchemaError("Signal table contains too many columns")
    columns = tuple(str(row.get("name") or "") for row in rows)
    if any(not _IDENTIFIER_RE.fullmatch(column) for column in columns):
        raise SchemaError("Signal schema contains an unsafe column name")
    return columns


def inspect_schema(reader: Any) -> SchemaInfo:
    tables = _tables(reader)
    if "messages" not in tables:
        raise SchemaError("Signal database has no messages table")
    message_columns = _columns(reader, "messages")
    conversation_columns = _columns(reader, "conversations") if "conversations" in tables else ()
    attachment_columns = _columns(reader, "message_attachments") if "message_attachments" in tables else ()
    if "type" not in message_columns and "json" not in message_columns:
        raise SchemaError("Signal messages cannot be classified safely")
    if "id" not in message_columns and "json" not in message_columns:
        raise SchemaError("Signal messages have no supported stable identifier")
    if "body" not in message_columns and "json" not in message_columns and not attachment_columns:
        raise SchemaError("Signal messages contain no supported content fields")
    if (
        not any(column in message_columns for column in ("timestamp", "sent_at", "received_at_ms"))
        and "json" not in message_columns
    ):
        raise SchemaError("Signal messages contain no supported timestamp")
    if attachment_columns and "messageId" not in attachment_columns:
        raise SchemaError("Signal attachment table has no message identifier")
    order_column = next(
        (column for column in ("received_at", "received_at_ms", "timestamp", "sent_at") if column in message_columns),
        "",
    )
    signature_payload = {
        "messages": message_columns,
        "conversations": conversation_columns,
        "message_attachments": attachment_columns,
        "order_column": order_column,
    }
    signature = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SchemaInfo(message_columns, conversation_columns, attachment_columns, order_column, signature)


_MESSAGE_COLUMNS = (
    "id",
    "conversationId",
    "type",
    "body",
    "sent_at",
    "timestamp",
    "received_at",
    "received_at_ms",
    "source",
    "sourceServiceId",
    "sourceUuid",
    "serverTimestamp",
)
_CONVERSATION_COLUMNS = (
    "id",
    "type",
    "name",
    "profileName",
    "profileFamilyName",
    "e164",
    "serviceId",
    "uuid",
    "aci",
    "pni",
    "groupId",
)
_ATTACHMENT_COLUMNS = (
    "messageId",
    "editHistoryIndex",
    "attachmentType",
    "orderInMessage",
    "contentType",
    "fileName",
    "size",
    "width",
    "height",
    "flags",
    "path",
)
_LEGACY_ATTACHMENT_FIELDS = (
    ("contentType", "content_type"),
    ("fileName", "file_name"),
    ("size", "size"),
    ("width", "width"),
    ("height", "height"),
    ("flags", "flags"),
    ("caption", "caption"),
)


def _json_extract(column: str, path: str) -> str:
    return f"CASE WHEN {column} IS NOT NULL AND json_valid({column}) THEN json_extract({column}, '{path}') END"


def _message_query(schema: SchemaInfo, cursor: dict[str, int] | None, limit: int) -> str:
    columns = set(schema.message_columns)
    pairs = ["'rowid', rowid"]
    for column in _MESSAGE_COLUMNS:
        if column in columns:
            pairs.append(f"'{column}', {column}")
    if "json" in columns:
        projections = {
            "json_id": "$.id",
            "json_body": "$.body",
            "json_conversation_id": "$.conversationId",
            "json_type": "$.type",
            "json_timestamp": "$.timestamp",
            "json_source": "$.source",
            "json_source_service_id": "$.sourceServiceId",
            "json_quote": "$.quote",
            "json_edit_history": "$.editHistory",
            "json_attachments": "$.attachments",
        }
        for alias, path in projections.items():
            pairs.append(f"'{alias}', {_json_extract('json', path)}")
    order_expression = f"COALESCE({schema.order_column}, -1)" if schema.order_column else "rowid"
    pairs.append(f"'cursor_order', {order_expression}")
    where = ""
    if cursor is not None:
        order_value = int(cursor["order"])
        rowid_value = int(cursor["rowid"])
        where = (
            f" WHERE ({order_expression} > {order_value}) OR "
            f"({order_expression} = {order_value} AND rowid > {rowid_value})"
        )
    return (
        f"SELECT json_object({', '.join(pairs)}) FROM messages{where} "
        f"ORDER BY {order_expression} ASC, rowid ASC LIMIT {int(limit)}"
    )


def _conversation_value(row: dict[str, Any], key: str) -> Any:
    direct = row.get(key)
    return direct if direct not in (None, "") else row.get("json_" + key)


def _message_identifier(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("json_id") or "")


def build_conversation_map(reader: Any, columns: Sequence[str]) -> dict[str, dict[str, Any]]:
    present = [column for column in _CONVERSATION_COLUMNS if column in columns]
    if "id" not in present:
        return {"by_id": {}, "by_service": {}}
    pairs = [f"'{column}', {column}" for column in present]
    if "json" in columns:
        for column in _CONVERSATION_COLUMNS:
            if column == "id":
                continue
            pairs.append(f"'json_{column}', {_json_extract('json', '$.' + column)}")
    rows = reader.query_json(
        f"SELECT json_object({', '.join(pairs)}) FROM conversations "
        f"ORDER BY id LIMIT {MAX_CONVERSATIONS + 1}"
    )
    if len(rows) > MAX_CONVERSATIONS:
        raise SchemaError("Signal conversation count exceeds its safety limit")
    by_id: dict[str, dict[str, str]] = {}
    by_service: dict[str, str] = {}
    for row in rows:
        conversation_id = str(row.get("id") or "")
        profile_name = " ".join(
            str(value).strip()
            for value in (_conversation_value(row, "profileName"), _conversation_value(row, "profileFamilyName"))
            if value and str(value).strip()
        )
        name = str(
            _conversation_value(row, "name")
            or profile_name
            or _conversation_value(row, "e164")
            or ""
        )
        raw_type = str(_conversation_value(row, "type") or "")
        conversation_type = "group" if raw_type == "group" else "dm" if raw_type == "private" else raw_type
        by_id[conversation_id] = {"name": name, "type": conversation_type}
        for key in ("serviceId", "uuid", "aci", "pni", "e164"):
            identifier = _conversation_value(row, key)
            if identifier:
                by_service[str(identifier)] = name
    return {"by_id": by_id, "by_service": by_service}


def _loads_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _loads_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _timestamp_ms(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _ms_to_iso(value: Any) -> str:
    milliseconds = _timestamp_ms(value)
    if milliseconds is None:
        return ""
    try:
        moment = dt.datetime.fromtimestamp(milliseconds / 1000, dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_attachments_root(path: Path | str | None) -> Path | None:
    if path is None:
        return None
    candidate = Path(path).expanduser()
    if not candidate.exists():
        return None
    if candidate.is_symlink() or not candidate.is_dir():
        raise SignalDesktopError("Signal attachments root is unsafe")
    try:
        return candidate.resolve(strict=True)
    except OSError:
        raise SignalDesktopError("Signal attachments root is unreadable") from None


def _safe_attachment_local_path(relative: Any, root: Path | None) -> Path | None:
    if not relative or root is None:
        return None
    text = str(relative)
    if "\x00" in text:
        return None
    path = Path(text)
    if path.is_absolute():
        candidate = path
    else:
        parts = path.parts
        if parts and parts[0] == "attachments.noindex":
            parts = parts[1:]
        if not parts or ".." in parts:
            return None
        candidate = root.joinpath(*parts)
    if _safe_attachment_file(candidate, root):
        try:
            return candidate.resolve(strict=True)
        except OSError:
            return None
    return None


def _legacy_attachments(value: Any, root: Path | None) -> list[dict[str, Any]]:
    raw = _loads_list(value)
    if len(raw) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise SchemaError("Signal attachment count exceeds its per-message safety limit")
    attachments: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        metadata: dict[str, Any] = {}
        for source, target in _LEGACY_ATTACHMENT_FIELDS:
            field = item.get(source)
            if field not in (None, ""):
                metadata[target] = field
        local_path = _safe_attachment_local_path(item.get("path"), root)
        if local_path is not None:
            metadata["local_path"] = str(local_path)
        attachments.append(metadata)
    return attachments


def _attachment_query(
    columns: Sequence[str], message_ids: Sequence[str], maximum_rows: int
) -> str:
    present = [column for column in _ATTACHMENT_COLUMNS if column in columns]
    pairs = ", ".join(f"'{column}', {column}" for column in present)
    quoted = ", ".join(_sql_text(message_id) for message_id in message_ids)
    order_columns = [
        column for column in ("messageId", "editHistoryIndex", "attachmentType", "orderInMessage") if column in columns
    ]
    order = ", ".join(order_columns) or "messageId"
    return (
        f"SELECT json_object({pairs}) FROM message_attachments "
        f"WHERE messageId IN ({quoted}) ORDER BY {order} LIMIT {int(maximum_rows)}"
    )


def _sql_text(value: str) -> str:
    if any(ord(character) < 0x20 for character in value):
        raise SchemaError("Signal identifier contains a control character")
    return "'" + value.replace("'", "''") + "'"


def _normalized_attachments_by_message(
    reader: Any,
    columns: Sequence[str],
    rows: Sequence[dict[str, Any]],
    root: Path | None,
) -> dict[str, list[dict[str, Any]]]:
    if not columns:
        return {}
    message_ids = list(
        dict.fromkeys(identifier for row in rows if (identifier := _message_identifier(row)))
    )
    if not message_ids:
        return {}
    maximum = min(MAX_QUERY_ROWS, len(message_ids) * MAX_ATTACHMENTS_PER_MESSAGE + 1)
    raw_rows = reader.query_json(_attachment_query(columns, message_ids, maximum))
    if len(raw_rows) >= maximum:
        raise SchemaError("Signal attachment count exceeds its per-batch safety limit")
    result: dict[str, list[dict[str, Any]]] = {}
    for row in raw_rows:
        message_id = str(row.get("messageId") or "")
        metadata: dict[str, Any] = {}
        field_map = {
            "editHistoryIndex": "edit_history_index",
            "attachmentType": "attachment_type",
            "orderInMessage": "order_in_message",
            "contentType": "content_type",
            "fileName": "file_name",
            "size": "size",
            "width": "width",
            "height": "height",
            "flags": "flags",
        }
        for source, target in field_map.items():
            field = row.get(source)
            if field not in (None, ""):
                metadata[target] = field
        local_path = _safe_attachment_local_path(row.get("path"), root)
        if local_path is not None:
            metadata["local_path"] = str(local_path)
        target = result.setdefault(message_id, [])
        target.append(metadata)
        if len(target) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise SchemaError("Signal attachment count exceeds its per-message safety limit")
    return result


def _extract_quote(value: Any) -> dict[str, str]:
    quote = _loads_dict(value)
    if not quote:
        return {}
    return {
        "id": str(quote.get("id") or ""),
        "author_id": str(quote.get("author") or quote.get("authorAci") or quote.get("authorUuid") or ""),
        "text": str(quote.get("text") or ""),
    }


def _extract_edit_history(value: Any) -> tuple[list[dict[str, Any]], int]:
    raw = _loads_list(value)
    selected: list[dict[str, Any]] = []
    for item in raw[-MAX_EDIT_VERSIONS:]:
        if not isinstance(item, dict):
            continue
        edit: dict[str, Any] = {}
        if item.get("body") is not None:
            edit["body"] = str(item.get("body") or "")
        timestamp = item.get("timestamp") or item.get("sent_at") or item.get("received_at_ms")
        if timestamp is not None:
            parsed = _timestamp_ms(timestamp)
            if parsed is not None:
                edit["timestamp_ms"] = parsed
        if edit:
            selected.append(edit)
    return selected, len(raw)


def map_message(
    row: dict[str, Any],
    conversations: dict[str, dict[str, Any]],
    attachments_root: Path | None,
    account: str,
    normalized_attachments: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    message_type = str(row.get("type") or row.get("json_type") or "").lower()
    direction = _signal_direction({"type": message_type})
    if direction not in {"inbound", "outbound"}:
        return None
    message_id = _message_identifier(row)
    if not message_id:
        raise SchemaError("Signal message has no supported stable identifier")
    body = str(row.get("body") if row.get("body") is not None else row.get("json_body") or "")
    legacy_attachments = _legacy_attachments(row.get("json_attachments"), attachments_root)
    attachments = list(normalized_attachments or legacy_attachments)
    edits, edit_count = _extract_edit_history(row.get("json_edit_history"))
    if not body and edits:
        body = str(edits[-1].get("body") or "")
    if not body.strip() and not attachments:
        return None
    conversation_id = str(row.get("conversationId") or row.get("json_conversation_id") or "")
    if not conversation_id:
        return None
    conversation = conversations["by_id"].get(conversation_id, {})
    source = str(
        row.get("sourceServiceId")
        or row.get("sourceUuid")
        or row.get("json_source_service_id")
        or row.get("source")
        or row.get("json_source")
        or ""
    )
    if direction == "outbound":
        sender_id = "self"
        sender_name = ""
    else:
        sender_id = source
        sender_name = conversations["by_service"].get(source, "") or source
    quote = _extract_quote(row.get("json_quote"))
    flags = ["edit"] if edit_count > 1 else []
    timestamp = (
        row.get("timestamp")
        or row.get("sent_at")
        or row.get("json_timestamp")
        or row.get("received_at_ms")
        or row.get("serverTimestamp")
    )
    provenance: dict[str, Any] = {
        "signal_desktop_id": message_id,
        "conversation_id": conversation_id,
        "rowid": row.get("rowid"),
    }
    if quote:
        provenance["quote"] = quote
    if edit_count:
        provenance["edit_history"] = edits
        provenance["edit_count"] = edit_count
        if edit_count > len(edits):
            provenance["edit_history_truncated"] = True
    return {
        "platform": "signal",
        "account": account,
        "conversation_id": conversation_id,
        "conversation_name": str(conversation.get("name") or ""),
        "conversation_type": str(conversation.get("type") or ""),
        "sender_id": sender_id,
        "sender_display_name": sender_name,
        "direction": direction,
        "body": body,
        "message_ts": _ms_to_iso(timestamp),
        "received_ts": _ms_to_iso(row.get("received_at_ms")),
        "source_message_id": "signal-desktop:" + message_id,
        "thread_id": quote.get("id", ""),
        "attachments": attachments,
        "flags": flags,
        "raw": provenance,
        "source_path": "signal-desktop",
    }


def _cursor_from_row(row: dict[str, Any]) -> dict[str, int]:
    try:
        return {"order": int(row.get("cursor_order")), "rowid": int(row.get("rowid"))}
    except (TypeError, ValueError, OverflowError):
        raise SchemaError("Signal message cursor has an unexpected shape") from None


def _valid_cursor(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        order = int(value["order"])
        rowid = int(value["rowid"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if rowid < 0:
        return None
    return {"order": order, "rowid": rowid}


def _upsert_batch(inbox: Any, events: Sequence[dict[str, Any]]) -> None:
    if not events:
        return
    batched = getattr(inbox, "upsert_events", None)
    if callable(batched):
        batched(events)
    else:
        for event in events:
            inbox.upsert_event(event)


def ingest_signal_desktop(
    reader: Any,
    inbox: Any,
    *,
    attachments_root: Path | str | None = None,
    account: str = SIGNAL_DESKTOP_ACCOUNT,
    full: bool = False,
    cursor: dict[str, int] | None = None,
    batch_limit: int = DEFAULT_BATCH_ROWS,
    lookback_rows: int = DEFAULT_LOOKBACK_ROWS,
    max_rows: int | None = None,
    schema: SchemaInfo | None = None,
) -> IngestResult:
    """Upsert full or keyset-incremental Signal history into Context Inbox."""

    if batch_limit < 1 or batch_limit > MAX_BATCH_ROWS:
        raise ValueError(f"batch_limit must be between 1 and {MAX_BATCH_ROWS}")
    if lookback_rows < 0 or lookback_rows > 10_000:
        raise ValueError("lookback_rows must be between 0 and 10000")
    if max_rows is not None and max_rows < 0:
        raise ValueError("max_rows must be nonnegative")
    schema = schema or inspect_schema(reader)
    conversations = build_conversation_map(reader, schema.conversation_columns)
    root = resolve_attachments_root(attachments_root)
    start_cursor = None if full else _valid_cursor(cursor)
    current = start_cursor
    cursor_history: deque[dict[str, int]] = deque(maxlen=max(1, lookback_rows + 1))
    if start_cursor is not None:
        cursor_history.append(start_cursor)
    imported = skipped = scanned = 0
    remaining = max_rows
    while True:
        limit = batch_limit if remaining is None else min(batch_limit, remaining)
        if limit <= 0:
            break
        rows = reader.query_json(_message_query(schema, current, limit))
        if not rows:
            break
        normalized = _normalized_attachments_by_message(
            reader, schema.attachment_columns, rows, root
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            event = map_message(
                row,
                conversations,
                root,
                account,
                normalized.get(_message_identifier(row)),
            )
            if event is None:
                skipped += 1
            else:
                events.append(event)
                imported += 1
            current = _cursor_from_row(row)
            cursor_history.append(current)
            scanned += 1
        _upsert_batch(inbox, events)
        if remaining is not None:
            remaining -= len(rows)
        if len(rows) < limit:
            break
    if lookback_rows == 0:
        resume_cursor = current
    elif len(cursor_history) > lookback_rows:
        resume_cursor = dict(cursor_history[0])
    elif start_cursor is not None:
        resume_cursor = start_cursor
    else:
        resume_cursor = None
    return IngestResult(
        imported=imported,
        skipped=skipped,
        scanned=scanned,
        cursor=current,
        resume_cursor=resume_cursor,
        order_col=schema.order_column,
        schema_signature=schema.signature,
    )


def prepare_signal_reader(
    *,
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    signal_db: Path | str = DEFAULT_SIGNAL_DB,
    attachments_root: Path | str | None = DEFAULT_ATTACHMENTS_ROOT,
    sqlcipher_path: str | None = None,
    login_keychain: Path | str | None = None,
    keychain_service: str = SAFE_STORAGE_SERVICE,
    keychain_account: str = SAFE_STORAGE_ACCOUNT,
    keychain_runner: Callable[..., Any] = subprocess.run,
    sqlcipher_runner: Callable[..., Any] | None = None,
    timeout: float = 120.0,
) -> PreparedSignalReader:
    config = read_signal_config(config_path)
    password: bytearray | None = None
    legacy = config.get("key")
    if isinstance(legacy, str) and _HEX64_RE.fullmatch(legacy.strip()):
        key_hex = derive_sqlcipher_key(config, b"")
    else:
        try:
            password = read_safe_storage_password(
                service=keychain_service,
                account=keychain_account,
                keychain=login_keychain,
                runner=keychain_runner,
                timeout=min(timeout, 30.0),
            )
            key_hex = derive_sqlcipher_key(config, password)
        finally:
            _zeroize(password)
    reader = SqlcipherProcessReader(
        signal_db,
        key_hex,
        sqlcipher_path=sqlcipher_path,
        runner=sqlcipher_runner,
        timeout=timeout,
    )
    try:
        validate_reader(reader)
        schema = inspect_schema(reader)
        resolved_attachments = resolve_attachments_root(attachments_root)
        return PreparedSignalReader(reader, schema, resolved_attachments)
    except Exception:
        reader.clear_key()
        raise


def _source_identity(path: Path | str) -> dict[str, int]:
    try:
        info = Path(path).expanduser().stat()
    except OSError:
        raise SignalDesktopError("Signal database identity is unavailable") from None
    return {"device": int(info.st_dev), "inode": int(info.st_ino)}


def load_collector_state(path: Path | str) -> dict[str, Any]:
    raw = _read_bounded_regular_file(path, MAX_STATE_BYTES, missing_ok=True)
    if raw is None:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SignalDesktopError("collector state is invalid; run once with --full") from None
    if not isinstance(value, dict):
        raise SignalDesktopError("collector state is invalid; run once with --full")
    return value


def save_collector_state(path: Path | str, value: dict[str, Any]) -> None:
    destination = Path(path).expanduser()
    if destination.is_symlink():
        raise SignalDesktopError("collector state path is unsafe")
    _prepare_private_file_parent(destination.parent)
    with atomic_text_writer(destination) as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    _chmod_private_file(destination)


@contextlib.contextmanager
def collector_lock(state_path: Path | str) -> Iterator[None]:
    state = Path(state_path).expanduser()
    _prepare_private_file_parent(state.parent)
    lock_path = state.with_name(state.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError:
        raise SignalDesktopError("collector lock path is unsafe") from None
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CollectorBusyError("another Signal Desktop collector is already running") from None
        yield
    finally:
        os.close(fd)


def _state_is_compatible(
    state: dict[str, Any], source_identity: dict[str, int], schema: SchemaInfo
) -> bool:
    if state.get("version") != 1 or state.get("source_identity") != source_identity:
        return False
    if state.get("schema_signature") != schema.signature:
        return False
    cursor = state.get("cursor")
    return cursor is None or _valid_cursor(cursor) is not None


def sync_signal_desktop(
    prepared: PreparedSignalReader,
    *,
    context_db: Path | str = DEFAULT_CONTEXT_DB,
    state_path: Path | str = DEFAULT_STATE_PATH,
    signal_db: Path | str = DEFAULT_SIGNAL_DB,
    force_full: bool = False,
    batch_limit: int = DEFAULT_BATCH_ROWS,
    lookback_rows: int = DEFAULT_LOOKBACK_ROWS,
    full_rescan_days: float = DEFAULT_FULL_RESCAN_DAYS,
    now: float | None = None,
) -> tuple[IngestResult, str]:
    if full_rescan_days < 0:
        raise ValueError("full_rescan_days must be nonnegative")
    stamp = time.time() if now is None else float(now)
    inbox = ContextInbox(context_db)
    with collector_lock(state_path):
        state = {} if force_full else load_collector_state(state_path)
        identity = _source_identity(signal_db)
        compatible = _state_is_compatible(state, identity, prepared.schema)
        last_full = float(state.get("last_full_at") or 0.0) if compatible else 0.0
        periodic_full = bool(
            full_rescan_days
            and compatible
            and stamp - last_full >= full_rescan_days * 24 * 60 * 60
        )
        full = force_full or not compatible or periodic_full
        cursor = None if full else _valid_cursor(state.get("cursor"))
        result = ingest_signal_desktop(
            prepared.reader,
            inbox,
            attachments_root=prepared.attachments_root,
            full=full,
            cursor=cursor,
            batch_limit=batch_limit,
            lookback_rows=lookback_rows,
            schema=prepared.schema,
        )
        new_state = {
            "version": 1,
            "source_identity": identity,
            "schema_signature": prepared.schema.signature,
            "order_column": prepared.schema.order_column,
            "cursor": result.resume_cursor,
            "highwater": result.cursor,
            "last_sync_at": stamp,
            "last_full_at": stamp if full else last_full,
        }
        save_collector_state(state_path, new_state)
        mode = "full" if full else "incremental"
        inbox.set_import_status(
            IMPORT_STATUS_SOURCE,
            "ok",
            f"mode={mode} scanned={result.scanned} imported={result.imported} skipped={result.skipped}",
        )
        return result, mode


def _safe_failure(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, CollectorBusyError):
        return "busy", "another collector instance is active"
    if isinstance(exc, KeychainError):
        return "blocked_keychain", "Safe Storage Keychain access failed"
    if isinstance(exc, DecryptionError):
        return "blocked_decryption", "Signal database key validation failed"
    if isinstance(exc, SchemaError):
        return "blocked_schema", "Signal Desktop schema is unsupported or exceeded a safety limit"
    if isinstance(exc, DependencyError):
        return "blocked_dependency", str(exc)
    if isinstance(exc, (ValueError, SignalDesktopError)):
        return "error", str(exc)
    return "error", "unexpected collector failure"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signal-desktop-history",
        description="Read-only macOS Signal Desktop history collector for Context Inbox.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate Keychain, key, SQLCipher, and schema without ingesting",
    )
    parser.add_argument("--full", action="store_true", help="ignore state and reconcile the full history")
    parser.add_argument("--json", action="store_true", help="print an aggregate-only machine-readable summary")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--signal-db", type=Path, default=DEFAULT_SIGNAL_DB)
    parser.add_argument("--attachments", type=Path, default=DEFAULT_ATTACHMENTS_ROOT)
    parser.add_argument("--context-db", "--db", dest="context_db", type=Path, default=DEFAULT_CONTEXT_DB)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--sqlcipher", default=None)
    parser.add_argument("--login-keychain", type=Path)
    parser.add_argument("--keychain-service", default=SAFE_STORAGE_SERVICE)
    parser.add_argument("--keychain-account", default=SAFE_STORAGE_ACCOUNT)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_ROWS)
    parser.add_argument("--lookback-rows", type=int, default=DEFAULT_LOOKBACK_ROWS)
    parser.add_argument("--full-rescan-days", type=float, default=DEFAULT_FULL_RESCAN_DAYS)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    keychain_runner: Callable[..., Any] = subprocess.run,
    sqlcipher_runner: Callable[..., Any] | None = None,
    now: Callable[[], float] = time.time,
) -> int:
    args = build_parser().parse_args(argv)
    if args.batch_size < 1 or args.batch_size > MAX_BATCH_ROWS:
        print(f"signal-desktop-history: --batch-size must be 1..{MAX_BATCH_ROWS}", file=sys.stderr)
        return 2
    if args.lookback_rows < 0 or args.lookback_rows > 10_000:
        print("signal-desktop-history: --lookback-rows must be 0..10000", file=sys.stderr)
        return 2
    if args.full_rescan_days < 0 or args.timeout <= 0:
        print("signal-desktop-history: timing values must be positive", file=sys.stderr)
        return 2
    prepared: PreparedSignalReader | None = None
    try:
        prepared = prepare_signal_reader(
            config_path=args.config,
            signal_db=args.signal_db,
            attachments_root=args.attachments,
            sqlcipher_path=args.sqlcipher,
            login_keychain=args.login_keychain,
            keychain_service=args.keychain_service,
            keychain_account=args.keychain_account,
            keychain_runner=keychain_runner,
            sqlcipher_runner=sqlcipher_runner,
            timeout=args.timeout,
        )
        if args.check:
            if args.json:
                print(
                    json.dumps(
                        {
                            "status": "ok",
                            "mode": "check",
                            "schema": prepared.schema.signature[:16],
                            "attachments_available": prepared.attachments_root is not None,
                        },
                        sort_keys=True,
                    )
                )
            return 0
        result, mode = sync_signal_desktop(
            prepared,
            context_db=args.context_db,
            state_path=args.state,
            signal_db=args.signal_db,
            force_full=args.full,
            batch_limit=args.batch_size,
            lookback_rows=args.lookback_rows,
            full_rescan_days=args.full_rescan_days,
            now=now(),
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "mode": mode,
                        "scanned": result.scanned,
                        "imported": result.imported,
                        "skipped": result.skipped,
                    },
                    sort_keys=True,
                )
            )
        return 0
    except Exception as exc:  # messages/bodies and subprocess diagnostics are deliberately suppressed
        status, message = _safe_failure(exc)
        if not args.check:
            try:
                ContextInbox(args.context_db).set_import_status(IMPORT_STATUS_SOURCE, status, message)
            except Exception:
                pass
        if args.json:
            print(json.dumps({"status": status, "error": message}, sort_keys=True), file=sys.stderr)
        else:
            print(f"signal-desktop-history: {message}", file=sys.stderr)
        return 1
    finally:
        if prepared is not None:
            prepared.clear()
