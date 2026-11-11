"""Private, atomic Dream reports and OpenViking-safe generated artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .config import DreamingConfig
from .redaction import contains_secret, redact

MANAGED_START = "<!-- hermes:dreaming:managed:start -->"
MANAGED_END = "<!-- hermes:dreaming:managed:end -->"


def validate_output_paths(config: DreamingConfig) -> None:
    for directory in (config.dream_state_db.parent, config.reports_dir, config.import_dir,
                      config.staging_dir, config.dreams_md.parent):
        _validate_destination(directory)
    for target in (config.dream_state_db, config.dreams_md):
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"configured Dream path is not a regular file: {target}")


def semantic_fingerprint(decisions: Sequence[dict[str, Any]], insights: Sequence[dict[str, Any]]) -> str:
    rows = [
        f"candidate|{item['candidate_id']}|{item['classification']}|{item['score']:.6f}|{','.join(sorted(item.get('evidence_refs', [])))}"
        for item in decisions
    ]
    rows.extend(
        f"insight|{item['kind']}|{item['claim']}|{item.get('status')}|{item.get('deep_supported')}|{','.join(_refs(item))}|{json.dumps(item.get('reflection', {}), sort_keys=True)}"
        for item in insights
    )
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()


def stage_success_artifacts(
    config: DreamingConfig,
    *,
    run_id: str,
    now: datetime,
    decisions: Sequence[dict[str, Any]],
    insights: Sequence[dict[str, Any]],
    quality: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any]:
    """Build a run's files outside the manifest-watched import directory."""

    staging_dir = _private_dir(config.staging_dir)
    reports_dir = _private_dir(config.reports_dir)
    import_dir = _private_dir(config.import_dir)
    dreams_dir = _private_dir(config.dreams_md.parent)
    if staging_dir.stat().st_dev != import_dir.stat().st_dev:
        raise ValueError("staging and watched import directories must share a filesystem")
    if staging_dir.stat().st_dev != reports_dir.stat().st_dev:
        raise ValueError("staging and report directories must share a filesystem")
    staged_report = staging_dir / f"{run_id}.report.md"
    staged_import = staging_dir / f"{run_id}.import.md"
    final_report = reports_dir / f"{run_id}.md"
    final_import = import_dir / f"{run_id}.md"
    report = render_report(run_id, now, decisions, insights, quality, config.budgets.max_report_items)
    indexed = render_indexed(run_id, now, decisions, insights, config.budgets.max_report_items)
    try:
        _write_regular(staged_report, report)
        _write_regular(staged_import, indexed)
        return {
            "staged_report": staged_report,
            "staged_import": staged_import,
            "final_report": final_report,
            "final_import": final_import,
            "report_hash": _file_hash(staged_report),
            "content_hash": _file_hash(staged_import),
            "fingerprint": fingerprint,
            "indexable": any(d["classification"] == "openviking_dream" for d in decisions) or any(
                i.get("deep_supported") is True and (
                    i.get("status") == "proposed" or (i.get("status") == "grounded" and
                    i.get("kind") not in {"contradiction", "stale_conflict", "hypothesis", "proposal"})
                ) for i in insights
            ),
        }
    except Exception:
        cleanup_staged((staged_report, staged_import))
        raise


def render_report(
    run_id: str,
    now: datetime,
    decisions: Sequence[dict[str, Any]],
    insights: Sequence[dict[str, Any]],
    quality: dict[str, Any],
    limit: int,
) -> str:
    durable_classifications = [
        d for d in decisions if d["classification"] == "openviking_dream"
    ]
    inbox = [d for d in decisions if d["classification"] == "context_inbox"]
    open_loops = [d for d in decisions if d["kind"] in {"open_loop", "commitment"}]
    hypotheses = [d for d in decisions if d["classification"] == "hypothesis"]
    deferred = [d for d in decisions if d["classification"] in {"defer", "reject"}]
    connections = [i for i in insights if i["kind"] in {"connection", "blind_spot"}]
    contradictions = [i for i in insights if i["kind"] in {"contradiction", "stale_conflict"}]
    proposals = [i for i in insights if i["kind"] == "proposal" and i.get("status") == "proposed"]
    lines = [
        f"# Dream-Reflexion · {now.strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        f"Dream-Run: `{run_id}`",
        "",
        "## Stärkste belastbare Erkenntnisse",
        "",
        *_decision_lines(
            durable_classifications,
            limit,
            "Keine neue belastbare OpenViking-Klassifikation in diesem Lauf.",
        ),
        "",
        "## Aktionskandidaten (nur privat; Freigabe erforderlich, keine Aktion)",
        "",
        *_decision_lines(inbox, limit, "Keine neuen Aktionskandidaten."),
        "",
        "## Verbindungen und blinde Flecken",
        "",
        *_insight_lines(connections, limit, "Keine ausreichend belegte neue Verbindung."),
        "",
        "## Konkrete Verbesserungsvorschläge (nicht ausgeführt)",
        "",
        *_proposal_lines(proposals, limit),
        "",
        "## Kurze Qualitätsprüfung",
        "",
        *_reflection_quality_lines(insights, limit),
        "",
        "## Offene Schleifen",
        "",
        *_decision_lines(open_loops, limit, "Keine neue offene Schleife."),
        "",
        "## Widersprüche (nicht stillschweigend aufgelöst)",
        "",
        *_insight_lines(contradictions, limit, "Keine belegten Widersprüche."),
        "",
        "## Hypothesen — keine Tatsachen",
        "",
        *_decision_lines(hypotheses, limit, "Keine neue Hypothese."),
        "",
        "## Zurückgestellt oder verworfen",
        "",
        *_decision_lines(deferred, limit, "Keine zurückgestellten Kandidaten."),
        "",
        "## Laufqualität",
        "",
        f"- Verarbeitete Sessions: {quality.get('sessions', 0)}",
        f"- Nicht lesbare konfigurierte Profile: {quality.get('profile_failures', 0)}",
        f"- Fehlgeschlagene OpenViking-Abfragen: {quality.get('retrieval_failures', 0)}",
        "- Rohtranskripte wurden nicht gespeichert; Belegauszüge sind redigiert und begrenzt.",
        "",
    ]
    return _safe("\n".join(lines) + "\n")


def render_indexed(
    run_id: str,
    now: datetime,
    decisions: Sequence[dict[str, Any]],
    insights: Sequence[dict[str, Any]],
    limit: int,
) -> str:
    durable = [d for d in decisions if d["classification"] == "openviking_dream"]
    visible_insights = [
        i for i in insights
        if i.get("status") == "grounded"
        and i.get("deep_supported") is True
        and i["kind"] not in {"contradiction", "stale_conflict", "hypothesis", "proposal"}
    ]
    lines = [
        "---",
        f'id: "{run_id}"',
        f'generated_at: "{now.isoformat()}"',
        'namespace: "dreams"',
        "---",
        "",
        "# Grounded Dream Consolidation",
        "",
        "Only redacted, deterministically grounded and Deep-supported durable conclusions.",
        "",
    ]
    for item in durable[:limit]:
        label = item["classification"]
        lines.extend(
            [
                f"## {label}: {item['claim']}",
                "",
                f"Evidence refs: {', '.join(f'`{ref}`' for ref in item['evidence_refs'][:12])}",
                f"Score: {item['score']:.4f}",
                "",
            ]
        )
    for item in visible_insights[:limit]:
        lines.extend(
            [
                f"## {item['kind']}: {item['claim']}",
                "",
                f"Evidence refs: {', '.join(f'`{ref}`' for ref in _refs(item)[:12])}",
                *_source_lines(item),
                "",
            ]
        )
    # Suggestions are retrievable, but never presented as durable user facts.
    proposals = [i for i in insights if i.get("kind") == "proposal"
                 and i.get("status") == "proposed" and i.get("deep_supported") is True]
    if proposals:
        lines.extend(["## Reviewed proposals - not executed, not user rules", ""])
        lines.extend(_proposal_lines(proposals, limit))
        lines.append("")
    return _safe("\n".join(lines) + "\n")


def _reflection_quality_lines(items: Sequence[dict[str, Any]], limit: int) -> list[str]:
    lines = []
    for item in items[:limit]:
        review = item.get("reflection", {}).get("quality_review")
        if not isinstance(review, dict) or not review:
            continue
        outcome = "nützlich" if (review.get("decision") == "useful" and item.get("deep_supported") is True
                                 and item.get("status") in {"grounded", "proposed"}) else "zurückgehalten"
        lines.append(f"- {item['claim']} - {outcome}: {review.get('summary', '')}")
    return lines or ["Keine neue geprüfte Idee; kein tägliches Ideensoll."]


def _proposal_lines(items: Sequence[dict[str, Any]], limit: int) -> list[str]:
    lines: list[str] = []
    for item in items[:limit]:
        reflection = item.get("reflection", {})
        lines.extend([
            f"- Vorschlag ({reflection.get('scope', 'unknown')}): {item['claim']}",
            f"  Möglicher nächster Schritt: {reflection.get('proposed_result', '')}",
            f"  Prüfen an: {reflection.get('expected_check', '')}",
            f"  Unsicherheit: {reflection.get('uncertainty', '')}",
            f"  Belege: {', '.join(_refs(item)[:12])}",
        ])
    return lines or ["Kein ausreichend belegter neuer Verbesserungsvorschlag."]


def update_dreams_md(path: Path, report: str, fingerprint: str) -> bool:
    _private_dir(path.parent)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError(f"DREAMS.md must be a regular file, not a symlink: {path}")
    existing = path.read_text(encoding="utf-8") if path.exists() else "# DREAMS\n\n"
    marker = f"<!-- dream-fingerprint:{fingerprint} -->"
    if marker in existing:
        return False
    entry = f"{marker}\n\n{report.strip()}"
    if MANAGED_START in existing or MANAGED_END in existing:
        if existing.count(MANAGED_START) != 1 or existing.count(MANAGED_END) != 1:
            raise ValueError("DREAMS.md has malformed managed markers")
        before, rest = existing.split(MANAGED_START, 1)
        managed, after = rest.split(MANAGED_END, 1)
        content = f"{before}{MANAGED_START}{managed.rstrip()}\n\n{entry}\n{MANAGED_END}{after}"
    else:
        content = f"{existing.rstrip()}\n\n{MANAGED_START}\n{entry}\n{MANAGED_END}\n"
    _write_regular(path, content)
    return True


def read_report(path: Path, *, max_characters: int = 50_000) -> str:
    _validate_destination(path.parent)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"report path is not a regular file: {path}")
    text = path.read_text(encoding="utf-8")
    return text if len(text) <= max_characters else text[: max_characters - 2].rstrip() + "…\n"


def remove_stale_run_artifacts(config: DreamingConfig, rows: Sequence[dict[str, Any]]) -> int:
    """Delete only exact retained-out report files, never watched imports."""

    removed = 0
    for row in rows:
        run_id = str(row.get("run_id") or "")
        if not re.fullmatch(r"run_[0-9a-f]{20}", run_id):
            continue
        root = config.reports_dir
        target = root / f"{run_id}.md"
        if (
            root.is_symlink()
            or str(row.get("path") or "") != str(target)
            or target.is_symlink()
            or not target.is_file()
        ):
            continue
        target.unlink()
        removed += 1
    return removed


def remove_stale_publication_artifacts(
    config: DreamingConfig, rows: Sequence[dict[str, Any]]
) -> int:
    """Delete exact watched imports only after their receipt-backed row was pruned."""

    removed = 0
    for row in rows:
        run_id = str(row.get("run_id") or "")
        if not re.fullmatch(r"run_[0-9a-f]{20}", run_id):
            continue
        target = config.import_dir / f"{run_id}.md"
        if (
            config.import_dir.is_symlink()
            or str(row.get("final_import_path") or "") != str(target)
            or not row.get("sync_source_id")
            or not row.get("sync_remote_resource_id")
            or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sync_receipt") or ""))
            or target.is_symlink()
            or not target.is_file()
        ):
            continue
        target.unlink()
        removed += 1
    return removed


def _decision_lines(items: Sequence[dict[str, Any]], limit: int, empty: str) -> list[str]:
    if not items:
        return [empty]
    return [
        f"- **{item['classification']}** · {item['claim']} — Score {item['score']:.4f}; {item['rationale']} "
        f"(Evidenz: {', '.join(item['evidence_refs'][:8])})"
        for item in items[:limit]
    ]


def _insight_lines(items: Sequence[dict[str, Any]], limit: int, empty: str) -> list[str]:
    if not items:
        return [empty]
    lines: list[str] = []
    for item in items[:limit]:
        status = str(item.get("status") or "hypothesis")
        sides = item.get("contradiction_sides") or []
        suffix = f"; Seiten: {' ↔ '.join(str(side) for side in sides[:2])}" if sides else ""
        lines.append(
            f"- **{status} · {item['kind']}** · {item['claim']} "
            f"(Evidenz: {', '.join(_refs(item)[:8])}{suffix})"
        )
        lines.extend(_source_lines(item))
    return lines


def _refs(item: dict[str, Any]) -> list[str]:
    evidence = item.get("evidence", [])
    return [str(entry.get("ref")) for entry in evidence if isinstance(entry, dict) and entry.get("ref")]


def _safe(text: str) -> str:
    value = redact(text)
    if contains_secret(value):
        raise ValueError("redaction invariant failed for Dream artifact")
    return value


def _private_dir(path: Path) -> Path:
    _validate_destination(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError(f"private path is not a directory: {path}")
    _chmod_owned(path, 0o700)
    return path


def _write_regular(path: Path, content: str) -> None:
    _validate_destination(path.parent)
    _private_dir(path.parent)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError(f"refusing to overwrite non-regular path: {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        _chmod_owned(path, 0o600)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _chmod_owned(path: Path, mode: int) -> None:
    try:
        if path.stat().st_uid == os.getuid():
            os.chmod(path, mode)
    except OSError:
        pass


def cleanup_staged(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            pass


def cleanup_uncommitted_staging(staging_dir: Path, committed_paths: set[str]) -> int:
    """Remove only exact run staging files that have no durable outbox row."""

    if not staging_dir.exists():
        return 0
    _private_dir(staging_dir)
    removed = 0
    pattern = re.compile(r"run_[0-9a-f]{20}\.(?:report|import)\.md")
    for path in staging_dir.iterdir():
        if not pattern.fullmatch(path.name) or str(path) in committed_paths:
            continue
        if path.is_file() and not path.is_symlink():
            path.unlink()
            removed += 1
    return removed


def publish_files(row: dict[str, Any], *, fault=None, fence=None) -> Path:
    """Publish one committed outbox row; the watched import move is last."""

    staged_report = Path(row["staged_report_path"])
    final_report = Path(row["final_report_path"])
    staged_import = Path(row["staged_import_path"])
    final_import = Path(row["final_import_path"])
    dreams_path = Path(row["dreams_path"])
    for directory in (final_report.parent, final_import.parent, dreams_path.parent):
        _private_dir(directory)
    if fence:
        fence("report publication")
    _publish_one(staged_report, final_report, str(row["report_hash"]))
    if fault:
        fault("after_report_publish")
    if fence:
        fence("report publication completion")
    report = read_report(final_report)
    update_dreams_md(dreams_path, report, str(row["fingerprint"]))
    if fault:
        fault("after_dreams_publish")
    if fence:
        fence("DREAMS publication completion")
    if not bool(row.get("indexable", True)):
        # Private reflection/report completion is distinct from index input.
        # A normal source scan cannot consult DreamStore, so never expose even
        # a heading-only file under its watched root. Reconcile an exact legacy
        # own import back into private staging if one was already moved there.
        if final_import.exists():
            if fence:
                fence("private-only import reconciliation")
            _publish_one(final_import, staged_import, str(row["content_hash"]))
            _fsync_dir(final_import.parent)
        elif staged_import.exists() and (staged_import.is_symlink() or not staged_import.is_file()
                                       or _file_hash(staged_import) != str(row["content_hash"])):
            raise ValueError("private-only staging artifact changed")
        return final_report
    # This is deliberately the final filesystem mutation: only a completely
    # ready artifact can become visible to the manifest watcher.
    if fence:
        fence("watched import publication")
    _publish_one(staged_import, final_import, str(row["content_hash"]))
    _fsync_dir(final_import.parent)
    if fault:
        fault("after_import_publish")
    return final_import


def _publish_one(staged: Path, final: Path, expected_hash: str) -> None:
    _validate_destination(staged.parent)
    _validate_destination(final.parent)
    if final.exists():
        if final.is_symlink() or not final.is_file() or _file_hash(final) != expected_hash:
            raise ValueError(f"publication target is not the committed artifact: {final}")
        if staged.exists() and staged.is_file() and not staged.is_symlink():
            staged.unlink()
        return
    if staged.is_symlink() or not staged.is_file() or _file_hash(staged) != expected_hash:
        raise ValueError("committed staging artifact is missing or changed")
    os.replace(staged, final)
    _chmod_owned(final, 0o600)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65_536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validate_destination(path: Path) -> None:
    absolute = path.absolute()
    missing = False
    for candidate in reversed((absolute, *absolute.parents)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            missing = True
            continue
        if missing:
            raise ValueError(f"path ancestry changed while checking: {candidate}")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"path ancestor is not a real directory: {candidate}")


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _source_lines(item: dict[str, Any]) -> list[str]:
    lines = []
    for entry in item.get("evidence", [])[:12]:
        provenance = entry.get("provenance") or {}
        if not provenance:
            continue
        uri = provenance.get("source_uri") or provenance.get("source_id") or "unknown"
        day = provenance.get("source_date") or "unknown"
        version = provenance.get("source_version") or "unknown"
        status = provenance.get("evidence_status") or "unknown"
        lines.append(f"Source {entry.get('ref', '')}: {uri}; source date: {day}; version: {version}; status: {status}")
    return lines
