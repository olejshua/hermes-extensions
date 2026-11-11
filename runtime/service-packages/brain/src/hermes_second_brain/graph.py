from __future__ import annotations

import argparse
import json
import mimetypes
import posixpath
import sqlite3
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlparse

from .manifest import Manifest, load_manifest

MAX_ACTIVE_RESOURCES = 500
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"


@dataclass(frozen=True)
class GraphServerOptions:
    manifest: Path
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True
    allow_remote: bool = False


def graph_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-second-brain graph")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--allow-remote", action="store_true")
    return parser


def options_from_args(args: argparse.Namespace) -> GraphServerOptions:
    return GraphServerOptions(
        manifest=args.manifest,
        host=args.host,
        port=args.port,
        open_browser=not bool(args.no_open),
        allow_remote=bool(args.allow_remote),
    )


def validate_bind(host: str, *, allow_remote: bool = False) -> None:
    if allow_remote or host in LOCAL_HOSTS:
        return
    raise ValueError(f"refusing non-loopback bind {host!r}; pass --allow-remote to override")


def main(argv: list[str] | None = None) -> int:
    parser = graph_arg_parser()
    args = parser.parse_args(argv)
    options = options_from_args(args)
    try:
        serve_graph(options)
    except ValueError as exc:
        parser.exit(2, f"graph: {exc}\n")
    return 0


def serve_graph(options: GraphServerOptions) -> None:
    validate_bind(options.host, allow_remote=options.allow_remote)
    manifest = load_manifest(options.manifest)
    handler = make_handler(manifest)
    server = ThreadingHTTPServer((options.host, options.port), handler)
    url = f"http://{options.host}:{server.server_address[1]}/"
    print(f"Memory Graph serving {url}")
    if options.open_browser:
        threading.Timer(0.25, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def make_handler(manifest: Manifest) -> type[BaseHTTPRequestHandler]:
    class MemoryGraphHandler(BaseHTTPRequestHandler):
        server_version = "HermesMemoryGraph/1.0"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_asset("index.html")
            elif parsed.path == "/api/graph":
                params = parse_qs(parsed.query, keep_blank_values=False)
                cap = _bounded_limit(params.get("limit", [str(MAX_ACTIVE_RESOURCES)])[0])
                self._send_json(build_memory_graph(manifest, resource_limit=cap))
            elif parsed.path == "/healthz":
                self._send_json({"ok": True})
            elif parsed.path.startswith("/assets/"):
                self._send_asset(parsed.path.removeprefix("/assets/"))
            else:
                self._send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_asset(self, name: str) -> None:
            safe_name = _safe_asset_name(name)
            if safe_name is None:
                self._send_error(HTTPStatus.NOT_FOUND)
                return
            asset = resources.files("hermes_second_brain").joinpath("assets", safe_name)
            if not asset.is_file():
                self._send_error(HTTPStatus.NOT_FOUND)
                return
            body = asset.read_bytes()
            content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
            if safe_name.endswith(".html"):
                content_type = "text/html; charset=utf-8"
            elif safe_name.endswith(".js"):
                content_type = "application/javascript; charset=utf-8"
            elif safe_name.endswith(".css"):
                content_type = "text/css; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: HTTPStatus) -> None:
            body = json.dumps({"error": status.phrase}).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

    return MemoryGraphHandler


def build_memory_graph(manifest: Manifest, *, resource_limit: int = MAX_ACTIVE_RESOURCES) -> dict[str, Any]:
    builder = _GraphBuilder()
    builder.add_node("root", "Hermes Second Brain", "root", "memory", size=28)
    for source in manifest.sources:
        source_id = f"source:{source.id}"
        builder.add_node(source_id, source.namespace, "source", "source", source_id=source.id, size=18)
        builder.add_link("root", source_id, "configured-source")

    truncated = False
    rows = _read_resource_rows(
        manifest.state_db,
        resource_limit + 1,
        allowed_source_root_ids={source.id for source in manifest.sources},
    )
    if len(rows) > resource_limit:
        rows = rows[:resource_limit]
        truncated = True
    folder_ids: dict[tuple[str, str], str] = {}
    for row in rows:
        source_id = f"source:{row['source_root_id']}"
        parent = source_id
        parts = [part for part in PurePosixPath(row["relative_path"]).parts if part not in {"", "."}]
        for folder in parts[:-1]:
            folder_key = (parent, folder)
            folder_id = folder_ids.setdefault(folder_key, builder.opaque_id("folder"))
            builder.add_node(folder_id, _public_path_part(folder), "folder", "folder", size=10)
            builder.add_link(parent, folder_id, "contains")
            parent = folder_id
        label = _public_path_part(parts[-1]) if parts else "resource"
        ext = Path(label).suffix.lower().lstrip(".") or "file"
        resource_id = builder.opaque_id("resource")
        builder.add_node(
            resource_id,
            label,
            "resource",
            ext,
            namespace=row["namespace"],
            status=row["status"],
            size_bytes=int(row["size_bytes"] or 0),
            size=7,
        )
        builder.add_link(parent, resource_id, "contains")

    context = _read_context_aggregates(manifest.context_inbox_db)
    if context["total_events"] or context["reminders"] or context["habits"]:
        context_id = "context-inbox"
        builder.add_node(context_id, "Context Inbox", "aggregate", "context", count=context["total_events"], size=17)
        builder.add_link("root", context_id, "private-aggregate")
        _add_aggregate_group(builder, context_id, "platform", "Platform", context["platforms"])
        _add_aggregate_group(builder, context_id, "tier", "Relevance", context["tiers"])
        _add_aggregate_group(builder, context_id, "reminder", "Reminders", context["reminders"])
        _add_aggregate_group(builder, context_id, "habit", "Habits", context["habits"])

    stats = {
        "resources": len(rows),
        "resource_limit": resource_limit,
        "truncated": truncated,
        "context_events": context["total_events"],
    }
    return {"nodes": builder.nodes, "links": builder.links, "stats": stats}


class _GraphBuilder:
    def __init__(self) -> None:
        self.nodes: list[dict[str, Any]] = []
        self.links: list[dict[str, str]] = []
        self.node_ids: set[str] = set()
        self.link_ids: set[tuple[str, str, str]] = set()
        self._opaque_counter = 0

    def opaque_id(self, prefix: str) -> str:
        self._opaque_counter += 1
        return f"{prefix}:{self._opaque_counter}"

    def add_node(self, node_id: str, label: str, kind: str, group: str, **extra: object) -> None:
        if node_id in self.node_ids:
            return
        self.node_ids.add(node_id)
        self.nodes.append({"id": node_id, "label": _safe_label(label), "kind": kind, "group": group, **extra})

    def add_link(self, source: str, target: str, kind: str) -> None:
        key = (source, target, kind)
        if key in self.link_ids:
            return
        self.link_ids.add(key)
        self.links.append({"source": source, "target": target, "kind": kind})


def _read_resource_rows(db_path: Path, limit: int, *, allowed_source_root_ids: set[str]) -> list[sqlite3.Row]:
    if not db_path.exists():
        return []
    if not allowed_source_root_ids:
        return []
    with _readonly_connection(db_path) as conn:
        if not _has_table(conn, "resources"):
            return []
        source_placeholders = ",".join("?" for _ in allowed_source_root_ids)
        return list(
            conn.execute(
                f"""
                SELECT source_id,source_root_id,namespace,relative_path,status,size_bytes
                FROM resources
                WHERE status!='deleted' AND source_root_id IN ({source_placeholders})
                ORDER BY namespace, relative_path, source_id
                LIMIT ?
                """,
                (*sorted(allowed_source_root_ids), limit),
            )
        )


def _read_context_aggregates(db_path: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {"total_events": 0, "platforms": {}, "tiers": {}, "reminders": {}, "habits": {}}
    if not db_path.exists():
        return empty
    with _readonly_connection(db_path) as conn:
        if _has_table(conn, "context_events"):
            empty["total_events"] = _scalar_count(conn, "context_events")
            empty["platforms"] = _counts(conn, "context_events", "platform")
            empty["tiers"] = _counts(conn, "context_events", "relevance_tier")
        if _has_table(conn, "context_reminders"):
            empty["reminders"] = _counts(conn, "context_reminders", "status")
        if _has_table(conn, "context_habit_hypotheses"):
            empty["habits"] = _counts(conn, "context_habit_hypotheses", "status")
    return empty


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _scalar_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _counts(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    rows = conn.execute(f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column} ORDER BY COUNT(*) DESC, {column}").fetchall()
    return {_safe_label(str(row[0] or "unknown")): int(row[1]) for row in rows}


def _add_aggregate_group(builder: _GraphBuilder, parent: str, key: str, label: str, counts: dict[str, int]) -> None:
    if not counts:
        return
    group_id = f"{parent}:{key}"
    total = sum(counts.values())
    builder.add_node(group_id, label, "aggregate", key, count=total, size=12)
    builder.add_link(parent, group_id, "summarizes")
    for value, count in counts.items():
        node_id = f"{group_id}:{_slug(value)}"
        builder.add_node(node_id, f"{value} ({count})", "aggregate", key, count=count, size=7 + min(16, count**0.5))
        builder.add_link(group_id, node_id, "summarizes")


def _safe_asset_name(name: str) -> str | None:
    normalized = posixpath.normpath("/" + name).lstrip("/")
    allowed = {"index.html", "app.js", "style.css"}
    if normalized in allowed and "/" not in normalized:
        return normalized
    return None


def _safe_label(value: str) -> str:
    text = " ".join(str(value).replace("\x00", "").split())
    return text[:120] if text else "unknown"


def _public_path_part(value: str) -> str:
    lowered = value.lower()
    sensitive_terms = (
        "token",
        "password",
        "passwd",
        "api-key",
        "apikey",
        "private-key",
        "oauth",
        "cookie",
        "credential",
        "secret",
        "auth-token",
        "access-key",
        ".env",
    )
    normalized = lowered.replace("_", "-").replace(" ", "-")
    if any(term in normalized for term in sensitive_terms):
        suffix = Path(value).suffix.lower()
        return f"[redacted]{suffix}" if suffix else "[redacted]"
    return _safe_label(value)


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:80] or "node"


def _bounded_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        return MAX_ACTIVE_RESOURCES
    return min(max(parsed, 1), MAX_ACTIVE_RESOURCES)
