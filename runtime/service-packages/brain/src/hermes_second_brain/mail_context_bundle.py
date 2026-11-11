from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context_inbox import _chmod_private_file, _prepare_private_file_parent, atomic_text_writer
from .mail_context_collector import DEFAULT_MAIL_OUTPUT_DIR, reject_symlink_ancestors

DEFAULT_MAIL_BUNDLE_DIR = Path("~/.hermes/second-brain/import/mail-bundles").expanduser()
MAIL_SOURCE_RE = re.compile(r"^mail-([0-9a-f]{32})\.md$")
EMAIL_ADDRESS_RE = re.compile(
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+"
    r"[A-Z]{2,63}",
    re.IGNORECASE,
)
SHARDS = "e000000000000007"


@dataclass
class BundleSummary:
    source_file_count: int = 0
    bundle_count: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: int = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "source_file_count": self.source_file_count,
            "bundle_count": self.bundle_count,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "errors": self.errors,
        }


def bundle_mail_context(input_dir: Path | str = DEFAULT_MAIL_OUTPUT_DIR, output_dir: Path | str = DEFAULT_MAIL_BUNDLE_DIR) -> BundleSummary:
    source_root = prepare_existing_input_dir(Path(input_dir))
    bundle_root = prepare_bundle_output_dir(Path(output_dir))
    summary = BundleSummary()
    shard_paths: dict[str, list[Path]] = {shard: [] for shard in SHARDS}

    for entry in sorted(source_root.iterdir(), key=lambda path: path.name):
        match = MAIL_SOURCE_RE.fullmatch(entry.name)
        if not match:
            summary.errors += 1
            continue
        if entry.is_symlink():
            summary.errors += 1
            shard_paths[match.group(1)[0]].append(entry)
            continue
        try:
            if not entry.is_file():
                summary.errors += 1
                shard_paths[match.group(1)[0]].append(entry)
                continue
        except OSError:
            summary.errors += 1
            shard_paths[match.group(1)[0]].append(entry)
            continue
        shard = match.group(1)[0]
        shard_paths[shard].append(entry)
        summary.source_file_count += 1

    for shard in SHARDS:
        target = bundle_root / f"mail-context-{shard}.md"
        sources = shard_paths[shard]
        if not sources:
            if target.is_symlink():
                raise ValueError("refusing symlinked aggregate bundle target")
            if target.exists():
                target.unlink()
                summary.removed += 1
            continue
        content = render_bundle(shard, sources, summary)
        if content is None:
            continue
        if target.is_symlink():
            raise ValueError("refusing symlinked aggregate bundle target")
        if target.exists() and target.read_text(encoding="utf-8") == content:
            _chmod_private_file(target)
            summary.unchanged += 1
        else:
            with atomic_text_writer(target) as fh:
                fh.write(content)
            summary.changed += 1
        summary.bundle_count += 1

    return summary


def render_bundle(shard: str, sources: list[Path], summary: BundleSummary) -> str | None:
    chunks = [
        "---",
        'source: "mail-context-bundles"',
        f'shard: "{shard}"',
        "---",
        "",
        "Sanitized aggregate mail context shard.",
        "",
    ]
    rendered_messages = 0
    shard_errors = 0
    for source in sources:
        if source.is_symlink():
            shard_errors += 1
            continue
        try:
            if not source.is_file():
                shard_errors += 1
                continue
        except OSError:
            shard_errors += 1
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            summary.errors += 1
            shard_errors += 1
            continue
        text = redact_bundle_chunk(text)
        if rendered_messages:
            chunks.append("<!-- mail-context-message-separator -->")
            chunks.append("")
        chunks.append(text)
        if not text.endswith("\n"):
            chunks.append("")
        rendered_messages += 1
    if shard_errors:
        return None
    return "\n".join(chunks)


def redact_bundle_chunk(text: str) -> str:
    return EMAIL_ADDRESS_RE.sub("[EMAIL]", text)


def prepare_existing_input_dir(input_dir: Path) -> Path:
    input_dir = input_dir.expanduser()
    reject_symlink_ancestors(input_dir)
    if input_dir.is_symlink():
        raise ValueError("input directory must not be a symlink")
    if not input_dir.is_dir():
        raise ValueError("input directory does not exist or is not a directory")
    return input_dir.resolve()


def prepare_bundle_output_dir(output_dir: Path) -> Path:
    output_dir = output_dir.expanduser()
    reject_symlink_ancestors(output_dir)
    if output_dir.exists() and output_dir.is_symlink():
        raise ValueError("output directory must not be a symlink")
    _prepare_private_file_parent(output_dir)
    reject_symlink_ancestors(output_dir)
    if not output_dir.is_dir():
        raise ValueError("output path is not a directory")
    return output_dir.resolve()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mail-context-bundle")
    parser.add_argument("--input-dir", type=Path, default=Path(os.environ.get("MAIL_CONTEXT_OUTPUT_DIR", str(DEFAULT_MAIL_OUTPUT_DIR))))
    parser.add_argument("--output-dir", type=Path, default=Path(os.environ.get("MAIL_CONTEXT_BUNDLE_DIR", str(DEFAULT_MAIL_BUNDLE_DIR))))
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        summary = bundle_mail_context(args.input_dir, args.output_dir)
    except Exception as exc:
        print(f"mail-context-bundle: {type(exc).__name__}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary.public_dict(), sort_keys=True))
    return 1 if summary.errors else 0
