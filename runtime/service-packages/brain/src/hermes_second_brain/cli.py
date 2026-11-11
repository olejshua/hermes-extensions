from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .briefing_preferences import (
    SECTIONS as BRIEFING_SECTIONS,
    default_preferences_path,
    load_preferences,
    reset_preferences,
    update_preferences,
)
from .context_inbox import (
    ContextInbox,
    db_from_args,
    default_context_export_path,
    import_canonical_jsonl,
    import_signal_desktop,
    import_slack_export,
    import_whatsapp_chatstorage,
)
from .intake import (
    MAX_BUNDLE_BYTES,
    decode_intake_bundle,
    load_intake_bundle,
    preview_intake_bundle,
    stage_intake_bundle,
)
from .ownership import DEFAULT_OWNERSHIP_PATH, validation_result
from .temporary_memory import TemporaryMemoryStore, public_temporary_record
from .eval import run_eval
from .dreaming import (
    acknowledge_dream_report,
    claim_dream_report,
    dream_report,
    dream_status,
    load_dreaming_config,
    release_dream_report,
    run_extended,
)
from .dreaming.config import parse_until
from .dreaming.redaction import redact
from .graph import options_from_args, serve_graph
from .lcm_export import export_lcm_summaries
from .logging_utils import configure_logging
from .mail_context_bundle import main as mail_context_bundle_main
from .mail_ingest import ingest_jsonl, materialize_markdown
from .mail_context_collector import main as mail_context_collector_main
from .manifest import load_manifest, write_default_manifest
from .migrate import migrate_holographic_db, migrate_markdown, write_jsonl, write_markdown_records
from .improvement import ImprovementStore, PROPOSAL_STATES
from .lifecycle import INTENT_STATES, LifecycleStore
from .observatory import (
    DEFAULT_MAX_ROWS,
    ObservatoryStore,
    import_spool,
    observatory_report,
)
from .sync import sync_manifest
from .context_sources import DEFAULT_SOURCE_DB, SourceRegistry, SourceRunner, SourceStatus, SourceStore, default_registry
from .context_sources.adapters.safe_import import SAFE_IMPORT_SOURCES, SafeImportAdapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-second-brain")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init-manifest")
    init.add_argument("path", type=Path)

    sync = sub.add_parser("sync")
    sync.add_argument("--manifest", type=Path, required=True)
    sync.add_argument("--dry-run", action="store_true")

    graph = sub.add_parser("graph")
    graph.add_argument("--manifest", type=Path, required=True)
    graph.add_argument("--host", default="127.0.0.1")
    graph.add_argument("--port", type=int, default=8765)
    graph.add_argument("--no-open", action="store_true")
    graph.add_argument("--allow-remote", action="store_true")

    migrate = sub.add_parser("migrate")
    migrate.add_argument("--memory-md", type=Path)
    migrate.add_argument("--user-md", type=Path)
    migrate.add_argument("--holographic-db", type=Path)
    migrate.add_argument("--output", type=Path)
    migrate.add_argument("--output-dir", type=Path)

    lcm = sub.add_parser("lcm-export")
    lcm.add_argument("--lcm-db", type=Path, action="append", required=True)
    lcm.add_argument("--output-dir", type=Path, required=True)

    mail = sub.add_parser("mail-ingest")
    mail.add_argument("--input", type=Path, required=True)
    mail.add_argument("--output", type=Path)
    mail.add_argument("--output-dir", type=Path)

    mail_context = sub.add_parser("mail-context-collect")
    mail_context.add_argument("--full", action="store_true")
    mail_context.add_argument("--account", action="append")
    mail_context.add_argument("--folder", action="append")
    mail_context.add_argument("--db", type=Path)
    mail_context.add_argument("--output-dir", type=Path)
    mail_context.add_argument("--himalaya")
    mail_context.add_argument("--page-size", type=int)
    mail_context.add_argument("--max-pages", type=int)
    mail_context.add_argument("--max-messages", type=int)
    mail_context.add_argument("--timeout", type=float)
    mail_context.add_argument("--workers", type=int)
    mail_context.add_argument("--retries", type=int)
    mail_context.add_argument("--retry-backoff", type=float)
    mail_context.add_argument("--json", action="store_true")

    mail_bundle = sub.add_parser("mail-context-bundle")
    mail_bundle.add_argument("--input-dir", type=Path)
    mail_bundle.add_argument("--output-dir", type=Path)
    mail_bundle.add_argument("--json", action="store_true")

    ev = sub.add_parser("eval")
    ev.add_argument("--spec", type=Path, required=True)
    ev.add_argument("--output", type=Path, required=True)
    ev.add_argument("--ov-binary", default="ov")
    ev.add_argument("--threshold", type=float, default=0.75)

    ci = sub.add_parser("context-import")
    ci.add_argument("--source", choices=("jsonl", "slack", "signal", "whatsapp"), required=True)
    ci.add_argument("--platform", default="generic", help="platform label for canonical JSONL imports")
    ci.add_argument("--path", type=Path, required=True)
    ci.add_argument("--db", type=Path)
    ci.add_argument("--manifest", type=Path)
    ci.add_argument("--dry-run", action="store_true")
    ci.add_argument("--json", action="store_true")

    cscan = sub.add_parser("context-scan")
    scan_sources = cscan.add_mutually_exclusive_group(required=True)
    scan_sources.add_argument("--source", action="append")
    scan_sources.add_argument("--all", action="store_true", dest="all_sources")
    cscan.add_argument("--full", action="store_true")
    cscan.add_argument("--dry-run", action="store_true")
    cscan.add_argument("--limit", type=int, default=1000)
    cscan.add_argument("--db", type=Path, help="Context Inbox derivative vault")
    cscan.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    cscan.add_argument("--ingress-dir", type=Path)
    cscan.add_argument("--json", action="store_true")

    chealth = sub.add_parser("context-source-health")
    chealth.add_argument("--source", action="append")
    chealth.add_argument("--ingress-dir", type=Path)
    chealth.add_argument("--json", action="store_true")

    cretention = sub.add_parser("context-retention")
    cretention.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    cretention.add_argument("--dry-run", action="store_true")
    cretention.add_argument("--json", action="store_true")

    cexport = sub.add_parser("context-import-export")
    cexport.add_argument("--source", choices=tuple(sorted(SAFE_IMPORT_SOURCES)), required=True)
    cexport.add_argument("--path", type=Path, required=True)
    cexport.add_argument("--format", choices=("json", "jsonl", "csv", "zip"), required=True)
    cexport.add_argument("--dry-run", action="store_true")
    cexport.add_argument("--limit", type=int, default=10000)
    cexport.add_argument("--db", type=Path, help="Context Inbox derivative vault")
    cexport.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    cexport.add_argument("--json", action="store_true")

    cr = sub.add_parser("context-rank")
    cr.add_argument("--db", type=Path)
    cr.add_argument("--manifest", type=Path)
    cr.add_argument("--json", action="store_true")

    cb = sub.add_parser("context-brief")
    cb.add_argument("--db", type=Path)
    cb.add_argument("--manifest", type=Path)
    cb.add_argument("--json", action="store_true")

    ce = sub.add_parser("context-export-openviking")
    ce.add_argument("--db", type=Path)
    ce.add_argument("--manifest", type=Path)
    ce.add_argument("--output", type=Path, default=default_context_export_path())
    ce.add_argument("--dry-run", action="store_true")
    ce.add_argument("--json", action="store_true")

    ca = sub.add_parser("context-alerts")
    ca.add_argument("--db", type=Path)
    ca.add_argument("--manifest", type=Path)
    ca.add_argument("--initialize", action="store_true")
    ca.add_argument("--prepared", action="store_true", help="Reuse ranks and reminders from a just-completed context export")
    ca.add_argument("--json", action="store_true")

    cf = sub.add_parser("context-feedback")
    cf.add_argument("--db", type=Path)
    cf.add_argument("--manifest", type=Path)
    cf.add_argument("--reminder", required=True)
    cf.add_argument("--status", choices=("done", "later", "dismissed", "candidate"), required=True)
    cf.add_argument("--json", action="store_true")

    cdb = sub.add_parser("context-daily-brief")
    cdb.add_argument("--db", type=Path)
    cdb.add_argument("--manifest", type=Path)
    cdb.add_argument("--hours", type=int, default=24)
    cdb.add_argument("--event-limit", type=int)
    cdb.add_argument("--reminder-limit", type=int)
    cdb.add_argument("--habit-limit", type=int)
    cdb.add_argument("--max-output-characters", type=int)
    cdb.add_argument("--include-section", choices=BRIEFING_SECTIONS, action="append")
    cdb.add_argument("--exclude-platform", action="append")
    cdb.add_argument("--preferences", type=Path)
    cdb.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Render preview output without claiming or emitting the daily notification. "
            "This is claim-safe, not write-free: it may refresh local derived rankings, "
            "reminder candidates, and habit hypotheses in the database."
        ),
    )
    cdb.add_argument(
        "--force",
        action="store_true",
        help="Bypass the once-per-day notification claim and always emit the brief.",
    )
    cdb.add_argument("--json", action="store_true")

    ownership = sub.add_parser("ownership-check")
    ownership.add_argument("--config", type=Path, default=DEFAULT_OWNERSHIP_PATH)
    ownership.add_argument("--json", action="store_true")

    temporary = sub.add_parser("temporary-memory")
    temporary_sub = temporary.add_subparsers(dest="temporary_cmd", required=True)
    temporary_add = temporary_sub.add_parser("add")
    temporary_add.add_argument("--db", type=Path)
    temporary_add.add_argument("--idempotency-key", required=True)
    temporary_add.add_argument("--kind", required=True)
    temporary_add.add_argument("--text", required=True)
    temporary_add.add_argument("--source-ref", default="")
    temporary_add.add_argument("--expires-at", required=True)
    temporary_add.add_argument("--now")
    temporary_add.add_argument("--json", action="store_true")
    temporary_list = temporary_sub.add_parser("list")
    temporary_list.add_argument("--db", type=Path)
    temporary_list.add_argument("--kind")
    temporary_list.add_argument("--include-expired", action="store_true")
    temporary_list.add_argument("--now")
    temporary_list.add_argument("--json", action="store_true")
    for operation in ("expire", "purge"):
        cleanup = temporary_sub.add_parser(operation)
        cleanup.add_argument("--db", type=Path)
        cleanup.add_argument("--now")
        cleanup.add_argument("--dry-run", action="store_true")
        cleanup.add_argument("--json", action="store_true")

    preferences = sub.add_parser("briefing-preferences")
    preferences_sub = preferences.add_subparsers(dest="preferences_cmd", required=True)
    preferences_show = preferences_sub.add_parser("show")
    preferences_show.add_argument("--path", type=Path)
    preferences_show.add_argument("--json", action="store_true")
    preferences_update = preferences_sub.add_parser("update")
    preferences_update.add_argument("--path", type=Path)
    preferences_update.add_argument("--include-section", choices=BRIEFING_SECTIONS, action="append")
    preferences_update.add_argument("--event-limit", type=int)
    preferences_update.add_argument("--reminder-limit", type=int)
    preferences_update.add_argument("--habit-limit", type=int)
    preferences_update.add_argument("--max-output-characters", type=int)
    quiet_group = preferences_update.add_mutually_exclusive_group()
    quiet_group.add_argument("--quiet-when-empty", dest="quiet_when_empty", action="store_true")
    quiet_group.add_argument("--show-when-empty", dest="quiet_when_empty", action="store_false")
    preferences_update.set_defaults(quiet_when_empty=None)
    excluded_group = preferences_update.add_mutually_exclusive_group()
    excluded_group.add_argument("--exclude-platform", action="append")
    excluded_group.add_argument("--clear-excluded-platforms", action="store_true")
    preferences_update.add_argument("--json", action="store_true")
    preferences_reset = preferences_sub.add_parser("reset")
    preferences_reset.add_argument("--path", type=Path)
    preferences_reset.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing custom target even if it is not valid preference content.",
    )
    preferences_reset.add_argument("--json", action="store_true")

    intake = sub.add_parser("intake-plan")
    intake.add_argument("--input", required=True, help="JSON file path or - for stdin")
    intake.add_argument("--db", type=Path)
    intake.add_argument("--now")
    intake_mode = intake.add_mutually_exclusive_group()
    intake_mode.add_argument("--commit", action="store_true")
    intake_mode.add_argument("--dry-run", action="store_true")
    intake.add_argument("--json", action="store_true")

    cs = sub.add_parser("context-stats")
    cs.add_argument("--db", type=Path)
    cs.add_argument("--manifest", type=Path)
    cs.add_argument("--json", action="store_true")

    lifecycle = sub.add_parser(
        "lifecycle", help="manage metadata-only intent and tool-job lifecycles"
    )
    lifecycle_sub = lifecycle.add_subparsers(dest="lifecycle_cmd", required=True)
    lifecycle_status = lifecycle_sub.add_parser("status")
    lifecycle_status.add_argument("--db", type=Path, required=True)
    lifecycle_status.add_argument("--json", action="store_true")
    intent_capture = lifecycle_sub.add_parser("intent-capture")
    intent_capture.add_argument("--db", type=Path, required=True)
    intent_capture.add_argument("--idempotency-key", required=True)
    intent_capture.add_argument("--summary", required=True)
    intent_capture.add_argument("--source", required=True)
    intent_capture.add_argument("--provenance-hash", default="")
    intent_capture.add_argument("--no-approval", action="store_true")
    intent_capture.add_argument("--now")
    intent_capture.add_argument("--json", action="store_true")
    intent_advance = lifecycle_sub.add_parser("intent-advance")
    intent_advance.add_argument("--db", type=Path, required=True)
    intent_advance.add_argument("--intent-id", required=True)
    intent_advance.add_argument("--state", choices=INTENT_STATES, required=True)
    intent_advance.add_argument("--idempotency-key")
    intent_advance.add_argument("--now")
    intent_advance.add_argument("--json", action="store_true")
    intent_list = lifecycle_sub.add_parser("intent-list")
    intent_list.add_argument("--db", type=Path, required=True)
    intent_list.add_argument("--state", choices=INTENT_STATES)
    intent_list.add_argument("--limit", type=int, default=100)
    intent_list.add_argument("--json", action="store_true")

    job_enqueue = lifecycle_sub.add_parser("job-enqueue")
    job_enqueue.add_argument("--db", type=Path, required=True)
    job_enqueue.add_argument("--idempotency-key", required=True)
    job_enqueue.add_argument("--job-kind", required=True)
    job_enqueue.add_argument("--intent-id")
    job_enqueue.add_argument("--max-attempts", type=int, default=3)
    job_enqueue.add_argument("--now")
    job_enqueue.add_argument("--json", action="store_true")
    job_start = lifecycle_sub.add_parser("job-start")
    job_start.add_argument("--db", type=Path, required=True)
    job_start.add_argument("--job-id", required=True)
    job_start.add_argument("--owner", required=True)
    job_start.add_argument("--lease-seconds", type=float, required=True)
    job_start.add_argument("--idempotency-key")
    job_start.add_argument("--now")
    job_start.add_argument("--json", action="store_true")
    job_resume = lifecycle_sub.add_parser("job-resume")
    job_resume.add_argument("--db", type=Path, required=True)
    job_resume.add_argument("--job-id", required=True)
    job_resume.add_argument("--owner", required=True)
    job_resume.add_argument("--lease-generation", type=int, required=True)
    job_resume.add_argument("--lease-seconds", type=float, required=True)
    job_resume.add_argument("--idempotency-key")
    job_resume.add_argument("--now")
    job_resume.add_argument("--json", action="store_true")
    job_heartbeat = lifecycle_sub.add_parser("job-heartbeat")
    job_heartbeat.add_argument("--db", type=Path, required=True)
    job_heartbeat.add_argument("--job-id", required=True)
    job_heartbeat.add_argument("--owner", required=True)
    job_heartbeat.add_argument("--lease-generation", type=int, required=True)
    job_heartbeat.add_argument("--lease-seconds", type=float, required=True)
    job_heartbeat.add_argument("--now")
    job_heartbeat.add_argument("--json", action="store_true")
    job_checkpoint = lifecycle_sub.add_parser("job-checkpoint")
    job_checkpoint.add_argument("--db", type=Path, required=True)
    job_checkpoint.add_argument("--job-id", required=True)
    job_checkpoint.add_argument("--owner", required=True)
    job_checkpoint.add_argument("--lease-generation", type=int, required=True)
    job_checkpoint.add_argument("--checkpoint", required=True)
    job_checkpoint.add_argument("--idempotency-key")
    job_checkpoint.add_argument("--now")
    job_checkpoint.add_argument("--json", action="store_true")
    job_verify = lifecycle_sub.add_parser("job-verify")
    job_verify.add_argument("--db", type=Path, required=True)
    job_verify.add_argument("--job-id", required=True)
    job_verify.add_argument("--owner", required=True)
    job_verify.add_argument("--lease-generation", type=int, required=True)
    job_verify.add_argument("--idempotency-key")
    job_verify.add_argument("--now")
    job_verify.add_argument("--json", action="store_true")
    job_complete = lifecycle_sub.add_parser("job-complete")
    job_complete.add_argument("--db", type=Path, required=True)
    job_complete.add_argument("--job-id", required=True)
    job_complete.add_argument("--owner", required=True)
    job_complete.add_argument("--lease-generation", type=int, required=True)
    job_complete.add_argument("--verification-evidence", required=True)
    job_complete.add_argument("--idempotency-key")
    job_complete.add_argument("--now")
    job_complete.add_argument("--json", action="store_true")
    job_fail = lifecycle_sub.add_parser("job-fail")
    job_fail.add_argument("--db", type=Path, required=True)
    job_fail.add_argument("--job-id", required=True)
    job_fail.add_argument("--owner", required=True)
    job_fail.add_argument("--lease-generation", type=int, required=True)
    job_fail.add_argument("--error-type", required=True)
    job_fail.add_argument("--idempotency-key")
    job_fail.add_argument("--now")
    job_fail.add_argument("--json", action="store_true")
    job_retry = lifecycle_sub.add_parser("job-retry")
    job_retry.add_argument("--db", type=Path, required=True)
    job_retry.add_argument("--job-id", required=True)
    job_retry.add_argument("--owner", required=True)
    job_retry.add_argument("--lease-generation", type=int, required=True)
    job_retry.add_argument("--idempotency-key")
    job_retry.add_argument("--now")
    job_retry.add_argument("--json", action="store_true")
    job_cancel = lifecycle_sub.add_parser("job-cancel")
    job_cancel.add_argument("--db", type=Path, required=True)
    job_cancel.add_argument("--job-id", required=True)
    job_cancel.add_argument("--owner")
    job_cancel.add_argument("--lease-generation", type=int)
    job_cancel.add_argument("--idempotency-key")
    job_cancel.add_argument("--now")
    job_cancel.add_argument("--json", action="store_true")
    lifecycle_stale = lifecycle_sub.add_parser("stale")
    lifecycle_stale.add_argument("--db", type=Path, required=True)
    lifecycle_stale.add_argument("--now")
    lifecycle_stale.add_argument("--dry-run", action="store_true")
    lifecycle_stale.add_argument("--json", action="store_true")

    observatory = sub.add_parser(
        "process-observatory", help="import and report bounded process metadata"
    )
    observatory_sub = observatory.add_subparsers(dest="observatory_cmd", required=True)
    observatory_report_parser = observatory_sub.add_parser("report")
    observatory_report_parser.add_argument("--db", type=Path, required=True)
    observatory_report_parser.add_argument("--top", type=int, default=5)
    observatory_report_parser.add_argument("--json", action="store_true")
    observatory_import = observatory_sub.add_parser("import")
    observatory_import.add_argument("--db", type=Path, required=True)
    observatory_import.add_argument("--spool", type=Path, required=True)
    observatory_import.add_argument("--quarantine", type=Path)
    observatory_import.add_argument("--retention-days", type=int, default=90)
    observatory_import.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    observatory_import.add_argument("--now")
    observatory_import.add_argument("--json", action="store_true")
    observatory_prune = observatory_sub.add_parser("prune")
    observatory_prune.add_argument("--db", type=Path, required=True)
    observatory_prune.add_argument("--retention-days", type=int, default=90)
    observatory_prune.add_argument("--now")
    observatory_prune.add_argument("--json", action="store_true")

    improvement = sub.add_parser(
        "improvement", help="manage review-gated process-improvement proposals"
    )
    improvement_sub = improvement.add_subparsers(dest="improvement_cmd", required=True)
    improvement_status = improvement_sub.add_parser("status")
    improvement_status.add_argument("--db", type=Path, required=True)
    improvement_status.add_argument("--json", action="store_true")
    improvement_propose = improvement_sub.add_parser("propose")
    improvement_propose.add_argument("--db", type=Path, required=True)
    improvement_propose.add_argument("--idempotency-key", required=True)
    improvement_propose.add_argument("--title", required=True)
    improvement_propose.add_argument("--risk-class", choices=("low", "medium", "high"), required=True)
    improvement_propose.add_argument(
        "--target-kind", choices=("skill", "config", "test", "workflow"), required=True
    )
    improvement_propose.add_argument("--intervention", required=True)
    improvement_propose.add_argument("--expected-metric", required=True)
    improvement_propose.add_argument("--observation-count", type=int, default=0)
    improvement_propose.add_argument("--evidence-count", type=int, default=0)
    improvement_propose.add_argument("--counterevidence-count", type=int, default=0)
    improvement_propose.add_argument("--rollback-note", default="")
    improvement_propose.add_argument("--now")
    improvement_propose.add_argument("--json", action="store_true")
    improvement_list = improvement_sub.add_parser("list")
    improvement_list.add_argument("--db", type=Path, required=True)
    improvement_list.add_argument("--state", choices=PROPOSAL_STATES)
    improvement_list.add_argument("--limit", type=int, default=100)
    improvement_list.add_argument("--json", action="store_true")
    improvement_transition = improvement_sub.add_parser("transition")
    improvement_transition.add_argument("--db", type=Path, required=True)
    improvement_transition.add_argument("--proposal-id", required=True)
    improvement_transition.add_argument(
        "--state",
        choices=("reviewed", "approved", "applied", "canary", "kept", "rolled_back", "rejected"),
        required=True,
    )
    improvement_transition.add_argument("--rollback-note")
    improvement_transition.add_argument("--now")
    improvement_transition.add_argument("--json", action="store_true")
    improvement_expire = improvement_sub.add_parser("expire")
    improvement_expire.add_argument("--db", type=Path, required=True)
    improvement_expire.add_argument("--ttl-days", type=int, default=30)
    improvement_expire.add_argument("--now")
    improvement_expire.add_argument("--dry-run", action="store_true")
    improvement_expire.add_argument("--json", action="store_true")

    dream = sub.add_parser("dream")
    dream.add_argument("--config", "--manifest", dest="config", type=Path, required=True)
    dream.add_argument("--full", action="store_true")
    dream.add_argument("--until")
    dream.add_argument("--interval-minutes", type=float)
    dream.add_argument("--max-rounds", type=int)
    dream.add_argument("--dry-run", action="store_true")
    dream.add_argument("--json", action="store_true")

    ds = sub.add_parser("dream-status")
    ds.add_argument("--config", "--manifest", dest="config", type=Path, required=True)
    ds.add_argument("--json", action="store_true")

    dr = sub.add_parser("dream-report")
    dr.add_argument("--config", "--manifest", dest="config", type=Path, required=True)
    dr.add_argument("--claim", action="store_true")
    dr.add_argument("--claim-owner")
    dr.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    if args.cmd == "init-manifest":
        write_default_manifest(args.path)
        return 0
    if args.cmd == "sync":
        summary = sync_manifest(load_manifest(args.manifest), dry_run=args.dry_run)
        if args.dry_run or args.verbose:
            print(summary)
        return 1 if summary.failed else 0
    if args.cmd == "lifecycle":
        try:
            store = LifecycleStore(args.db)
            operation = args.lifecycle_cmd
            if operation == "status":
                result = store.snapshot()
            elif operation == "intent-capture":
                result = _public_intent(
                    store.capture_intent(
                        idempotency_key=args.idempotency_key,
                        summary=args.summary,
                        source=args.source,
                        provenance_hash=args.provenance_hash,
                        requires_approval=not args.no_approval,
                        now=args.now,
                    )
                )
            elif operation == "intent-advance":
                result = _public_intent(
                    store.advance_intent(
                        args.intent_id,
                        args.state,
                        idempotency_key=args.idempotency_key,
                        now=args.now,
                    )
                )
            elif operation == "intent-list":
                result = {
                    "intents": [
                        _public_intent(item)
                        for item in store.list_intents(state=args.state, limit=args.limit)
                    ]
                }
            elif operation == "job-enqueue":
                result = _public_job(
                    store.enqueue_job(
                        idempotency_key=args.idempotency_key,
                        job_kind=args.job_kind,
                        intent_id=args.intent_id,
                        max_attempts=args.max_attempts,
                        now=args.now,
                    )
                )
            elif operation == "job-start":
                result = _public_job(
                    store.start_job(
                        args.job_id,
                        owner=args.owner,
                        lease_seconds=args.lease_seconds,
                        idempotency_key=args.idempotency_key,
                        now=args.now,
                    )
                )
            elif operation == "job-resume":
                result = _public_job(
                    store.resume_job(
                        args.job_id,
                        owner=args.owner,
                        lease_generation=args.lease_generation,
                        lease_seconds=args.lease_seconds,
                        idempotency_key=args.idempotency_key,
                        now=args.now,
                    )
                )
            elif operation == "job-heartbeat":
                result = _public_job(
                    store.heartbeat(
                        args.job_id,
                        owner=args.owner,
                        lease_generation=args.lease_generation,
                        lease_seconds=args.lease_seconds,
                        now=args.now,
                    )
                )
            elif operation == "job-checkpoint":
                result = _public_job(
                    store.checkpoint_job(
                        args.job_id,
                        owner=args.owner,
                        lease_generation=args.lease_generation,
                        checkpoint=args.checkpoint,
                        idempotency_key=args.idempotency_key,
                        now=args.now,
                    )
                )
            elif operation == "job-verify":
                result = _public_job(
                    store.begin_verification(
                        args.job_id,
                        owner=args.owner,
                        lease_generation=args.lease_generation,
                        idempotency_key=args.idempotency_key,
                        now=args.now,
                    )
                )
            elif operation == "job-complete":
                result = _public_job(
                    store.complete_job(
                        args.job_id,
                        owner=args.owner,
                        lease_generation=args.lease_generation,
                        verification_evidence=args.verification_evidence,
                        idempotency_key=args.idempotency_key,
                        now=args.now,
                    )
                )
            elif operation == "job-fail":
                result = _public_job(
                    store.fail_job(
                        args.job_id,
                        owner=args.owner,
                        lease_generation=args.lease_generation,
                        error_type=args.error_type,
                        idempotency_key=args.idempotency_key,
                        now=args.now,
                    )
                )
            elif operation == "job-retry":
                result = _public_job(
                    store.retry_job(
                        args.job_id,
                        owner=args.owner,
                        lease_generation=args.lease_generation,
                        idempotency_key=args.idempotency_key,
                        now=args.now,
                    )
                )
            elif operation == "job-cancel":
                result = _public_job(
                    store.cancel_job(
                        args.job_id,
                        owner=args.owner,
                        lease_generation=args.lease_generation,
                        idempotency_key=args.idempotency_key,
                        now=args.now,
                    )
                )
            elif operation == "stale":
                result = store.detect_stale_jobs(now=args.now, dry_run=args.dry_run)
            else:  # pragma: no cover - argparse owns this invariant
                raise AssertionError(operation)
        except Exception as exc:
            print(f"lifecycle: {_safe_cli_error(exc)}", file=sys.stderr)
            return 2
        print(_json(result))
        return 0
    if args.cmd == "process-observatory":
        try:
            store = ObservatoryStore(args.db)
            if args.observatory_cmd == "report":
                if not 1 <= args.top <= 100:
                    raise ValueError("--top must be between 1 and 100")
                result = observatory_report(store, top=args.top)
            elif args.observatory_cmd == "import":
                result = import_spool(
                    store,
                    args.spool,
                    quarantine_dir=args.quarantine,
                    now=args.now,
                )
                result["pruned"] = store.prune(
                    now=args.now, retention_days=args.retention_days
                )
                # Absolute row cap runs after TTL pruning; keeps the store bounded.
                result["capped"] = store.enforce_row_cap(args.max_rows)
            elif args.observatory_cmd == "prune":
                result = {
                    "pruned": store.prune(
                        now=args.now, retention_days=args.retention_days
                    )
                }
            else:  # pragma: no cover
                raise AssertionError(args.observatory_cmd)
        except Exception as exc:
            print(f"process-observatory: {_safe_cli_error(exc)}", file=sys.stderr)
            return 2
        print(_json(result))
        return 0
    if args.cmd == "improvement":
        try:
            store = ImprovementStore(args.db)
            operation = args.improvement_cmd
            if operation == "status":
                result = store.snapshot()
            elif operation == "propose":
                result = _public_improvement(
                    store.propose(
                        idempotency_key=args.idempotency_key,
                        title=args.title,
                        risk_class=args.risk_class,
                        target_kind=args.target_kind,
                        proposed_intervention=args.intervention,
                        expected_metric=args.expected_metric,
                        observation_count=args.observation_count,
                        evidence_count=args.evidence_count,
                        counterevidence_count=args.counterevidence_count,
                        rollback_note=args.rollback_note,
                        now=args.now,
                    )
                )
            elif operation == "list":
                result = {
                    "proposals": [
                        _public_improvement(item)
                        for item in store.list(state=args.state, limit=args.limit)
                    ]
                }
            elif operation == "transition":
                methods = {
                    "reviewed": store.review,
                    "approved": store.approve,
                    "applied": store.apply,
                    "canary": store.start_canary,
                    "kept": store.keep,
                    "rejected": store.reject,
                }
                if args.state == "rolled_back":
                    if not args.rollback_note:
                        raise ValueError("rolled_back requires --rollback-note")
                    changed = store.roll_back(
                        args.proposal_id,
                        rollback_note=args.rollback_note,
                        now=args.now,
                    )
                else:
                    changed = methods[args.state](args.proposal_id, now=args.now)
                result = _public_improvement(changed)
            elif operation == "expire":
                expired = store.expire_stale(
                    now=args.now, ttl_days=args.ttl_days, dry_run=args.dry_run
                )
                result = {
                    "operation": expired["operation"],
                    "dry_run": expired["dry_run"],
                    "expired_count": len(expired["expired"]),
                    "proposal_ids": expired["expired"],
                }
            else:  # pragma: no cover
                raise AssertionError(operation)
        except Exception as exc:
            print(f"improvement: {_safe_cli_error(exc)}", file=sys.stderr)
            return 2
        print(_json(result))
        return 0
    if args.cmd == "dream":
        try:
            config = load_dreaming_config(args.config)
            now = datetime.now(ZoneInfo(config.timezone))
            deadline = parse_until(args.until, now=now, tz=config.tzinfo()) if args.until else None
            if args.max_rounds is not None and args.max_rounds < 1:
                raise ValueError("--max-rounds must be at least 1")
            if args.interval_minutes is not None and args.interval_minutes < 0:
                raise ValueError("--interval-minutes must not be negative")
            result = run_extended(
                config,
                full=args.full,
                until=deadline,
                interval_minutes=args.interval_minutes,
                max_rounds=(
                    args.max_rounds
                    if args.max_rounds is not None
                    else config.max_rounds
                ),
                dry_run=args.dry_run,
            )
        except Exception as exc:
            print(f"dream: {_safe_cli_error(exc)}", file=sys.stderr)
            return 2
        if args.json or args.verbose:
            print(_json(result.as_dict()))
        return 1 if result.status == "failed" else 0
    if args.cmd == "dream-status":
        try:
            config = load_dreaming_config(args.config)
            status = dream_status(config)
        except Exception as exc:
            print(f"dream-status: {_safe_cli_error(exc)}", file=sys.stderr)
            return 1
        if args.json:
            print(_json(status))
        else:
            last = status.get("last_run") or {}
            print(f"enabled={str(status.get('enabled', False)).lower()} active={str(bool(status.get('active'))).lower()} last={last.get('status', 'never')} phase={last.get('last_phase') or '-'} openviking_classified={status.get('openviking_classified', 0)} failures={status.get('failures', 0)}")
        return 0
    if args.cmd == "dream-report":
        delivery = None
        try:
            config = load_dreaming_config(args.config)
            if args.claim:
                delivery = claim_dream_report(config, owner=args.claim_owner)
                report = delivery.text if delivery else None
            else:
                report = dream_report(config)
            if args.json:
                print(_json({"available": report is not None, "claimed": bool(delivery), "report": report}))
            elif report is not None:
                print(report, end="" if report.endswith("\n") else "\n")
            sys.stdout.flush()
            if delivery is not None and not acknowledge_dream_report(config, delivery):
                raise RuntimeError("report acknowledgement fence was lost")
        except Exception as exc:
            if delivery is not None:
                try:
                    release_dream_report(config, delivery)
                except Exception:
                    pass
            print(f"dream-report: {_safe_cli_error(exc)}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "graph":
        try:
            serve_graph(options_from_args(args))
        except ValueError as exc:
            print(f"graph: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "migrate":
        if not args.output and not args.output_dir:
            parser.error("migrate requires --output and/or --output-dir")
        records = []
        if args.memory_md:
            records.extend(migrate_markdown(args.memory_md, "MEMORY.md"))
        if args.user_md:
            records.extend(migrate_markdown(args.user_md, "USER.md"))
        if args.holographic_db:
            records.extend(migrate_holographic_db(args.holographic_db))
        counts = []
        try:
            if args.output:
                counts.append(("jsonl", write_jsonl(records, args.output)))
            if args.output_dir:
                counts.append(("markdown", write_markdown_records(records, args.output_dir)))
        except Exception as exc:
            print(f"migrate: {exc}", file=sys.stderr)
            return 1
        if args.verbose:
            print(" ".join(f"{name}={count}" for name, count in counts))
        return 0
    if args.cmd == "lcm-export":
        try:
            count = export_lcm_summaries(args.lcm_db, args.output_dir)
        except Exception as exc:
            print(f"lcm-export: {exc}", file=sys.stderr)
            return 1
        if args.verbose:
            print(count)
        return 0
    if args.cmd == "mail-ingest":
        if not args.output and not args.output_dir:
            parser.error("mail-ingest requires --output and/or --output-dir")
        counts = []
        try:
            if args.output:
                counts.append(("jsonl", ingest_jsonl(args.input, args.output)))
            if args.output_dir:
                counts.append(("markdown", materialize_markdown(args.input, args.output_dir)))
        except Exception as exc:
            print(f"mail-ingest: {exc}", file=sys.stderr)
            return 1
        if args.verbose:
            print(" ".join(f"{name}={count}" for name, count in counts))
        return 0
    if args.cmd == "mail-context-collect":
        forwarded = []
        if args.full:
            forwarded.append("--full")
        for account in args.account or []:
            forwarded.extend(["--account", account])
        for folder in args.folder or []:
            forwarded.extend(["--folder", folder])
        if args.db:
            forwarded.extend(["--db", str(args.db)])
        if args.output_dir:
            forwarded.extend(["--output-dir", str(args.output_dir)])
        if args.himalaya:
            forwarded.extend(["--himalaya", args.himalaya])
        if args.page_size is not None:
            forwarded.extend(["--page-size", str(args.page_size)])
        if args.max_pages is not None:
            forwarded.extend(["--max-pages", str(args.max_pages)])
        if args.max_messages is not None:
            forwarded.extend(["--max-messages", str(args.max_messages)])
        if args.timeout is not None:
            forwarded.extend(["--timeout", str(args.timeout)])
        if args.workers is not None:
            forwarded.extend(["--workers", str(args.workers)])
        if args.retries is not None:
            forwarded.extend(["--retries", str(args.retries)])
        if args.retry_backoff is not None:
            forwarded.extend(["--retry-backoff", str(args.retry_backoff)])
        if args.json:
            forwarded.append("--json")
        return mail_context_collector_main(forwarded)
    if args.cmd == "mail-context-bundle":
        forwarded = []
        if args.input_dir:
            forwarded.extend(["--input-dir", str(args.input_dir)])
        if args.output_dir:
            forwarded.extend(["--output-dir", str(args.output_dir)])
        if args.json:
            forwarded.append("--json")
        return mail_context_bundle_main(forwarded)
    if args.cmd == "eval":
        return run_eval(args.spec, args.output, args.ov_binary, args.threshold)
    if args.cmd == "ownership-check":
        result = validation_result(args.config)
        if args.json:
            print(_json(result))
        elif result["valid"]:
            print(
                f"Ownership map valid: schema={result['schema_version']} "
                f"categories={result['categories']} path={result['path']}"
            )
        else:
            print(f"Ownership map invalid: {result['path']}", file=sys.stderr)
            for error in result["errors"]:
                print(f"- {_safe_cli_error(ValueError(error))}", file=sys.stderr)
        return 0 if result["valid"] else 2
    if args.cmd == "temporary-memory":
        try:
            store = TemporaryMemoryStore(db_from_args(args, load_manifest))
            if args.temporary_cmd == "add":
                record = store.add(
                    idempotency_key=args.idempotency_key,
                    kind=args.kind,
                    text=args.text,
                    source_ref=args.source_ref,
                    expires_at=args.expires_at,
                    now=args.now,
                )
                public = public_temporary_record(record)
                if args.json:
                    print(_json(public))
                else:
                    verb = "Added" if record["created"] else "Already present"
                    print(f"{verb}: {record['record_id']} ({record['kind']}, expires {record['expires_at']})")
                return 0
            if args.temporary_cmd == "list":
                records = [
                    public_temporary_record(record)
                    for record in store.list(
                        now=args.now,
                        include_expired=args.include_expired,
                        kind=args.kind,
                    )
                ]
                if args.json:
                    print(_json({"records": records, "count": len(records), "include_expired": args.include_expired}))
                else:
                    for record in records:
                        print(
                            f"{record['record_id']} {record['kind']} {record['status']} "
                            f"expires={record['expires_at']} {record['text']}"
                        )
                return 0
            operation = getattr(store, args.temporary_cmd)
            result = operation(now=args.now, dry_run=args.dry_run)
            if args.json:
                print(_json(result))
            else:
                preview = "Would match" if args.dry_run else "Matched"
                print(f"{preview} {result['matched']} temporary records for {args.temporary_cmd}.")
            return 0
        except Exception as exc:
            print(f"temporary-memory: {_safe_cli_error(exc)}", file=sys.stderr)
            return 2
    if args.cmd == "briefing-preferences":
        target = args.path if args.path is not None else default_preferences_path()
        try:
            if args.preferences_cmd == "show":
                preferences_value = load_preferences(target)
            elif args.preferences_cmd == "reset":
                # Preserve the distinction between the canonical implicit
                # default and an explicit path. An environment override is
                # detected inside reset_preferences and is also custom.
                preferences_value = reset_preferences(
                    args.path,
                    force=getattr(args, "force", False),
                )
            else:
                updates: dict[str, object] = {}
                if args.include_section is not None:
                    updates["included_sections"] = args.include_section
                limits = {
                    key: value
                    for key, value in (
                        ("events", args.event_limit),
                        ("reminders", args.reminder_limit),
                        ("habits", args.habit_limit),
                    )
                    if value is not None
                }
                if limits:
                    updates["limits"] = limits
                if args.max_output_characters is not None:
                    updates["max_output_characters"] = args.max_output_characters
                if args.quiet_when_empty is not None:
                    updates["quiet_when_empty"] = args.quiet_when_empty
                if args.clear_excluded_platforms:
                    updates["excluded_platforms"] = []
                elif args.exclude_platform is not None:
                    updates["excluded_platforms"] = args.exclude_platform
                if not updates:
                    raise ValueError("update requires at least one preference option")
                preferences_value = update_preferences(updates, target)
        except Exception as exc:
            print(f"briefing-preferences: {_safe_cli_error(exc)}", file=sys.stderr)
            return 2
        result = {
            "path": str(Path(target).expanduser()),
            "exists": Path(target).expanduser().exists(),
            "preferences": preferences_value.as_dict(),
        }
        if args.json:
            print(_json(result))
        else:
            values = result["preferences"]
            print(f"Briefing preferences: {result['path']}")
            print(f"sections={','.join(values['included_sections']) or '-'}")
            print(
                "limits="
                + ",".join(f"{key}:{value}" for key, value in values["limits"].items())
                + f" max_output_characters={values['max_output_characters']}"
            )
            print(
                f"quiet_when_empty={str(values['quiet_when_empty']).lower()} "
                f"excluded_platforms={','.join(values['excluded_platforms']) or '-'}"
            )
        return 0
    if args.cmd == "intake-plan":
        try:
            if args.input == "-":
                # Bound stdin in BYTES (not decoded characters) so a multibyte
                # UTF-8 payload cannot exceed the 512 KiB limit, then decode
                # strictly as UTF-8.
                buffer = getattr(sys.stdin, "buffer", None)
                if buffer is not None:
                    raw_bytes = buffer.read(MAX_BUNDLE_BYTES + 1)
                else:  # pragma: no cover - stdin without a binary buffer
                    raw_bytes = sys.stdin.read(MAX_BUNDLE_BYTES + 1).encode("utf-8", "surrogatepass")
                if len(raw_bytes) > MAX_BUNDLE_BYTES:
                    raise ValueError("stdin intake JSON exceeds 512 KiB")
                try:
                    value = raw_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("stdin intake JSON must be valid UTF-8") from exc
                bundle = decode_intake_bundle(value)
            else:
                bundle = load_intake_bundle(Path(args.input))
            if args.commit:
                result = stage_intake_bundle(
                    bundle,
                    db_path=db_from_args(args, load_manifest),
                    now=args.now,
                )
            else:
                result = preview_intake_bundle(bundle, now=args.now)
            result["dry_run"] = not args.commit
        except Exception as exc:
            error = {"ok": False, "error": _safe_cli_error(exc), "committed": False, "executed": False}
            if args.json:
                print(_json(error))
            else:
                print(f"intake-plan: {error['error']}", file=sys.stderr)
            return 2
        if args.json:
            print(_json(result))
        else:
            print(result["confirmation"])
        return 0
    if args.cmd == "context-import":
        db = db_from_args(args, load_manifest)
        try:
            if args.source == "jsonl":
                summary = import_canonical_jsonl(args.path, db, platform=args.platform, dry_run=args.dry_run)
            elif args.source == "slack":
                summary = import_slack_export(args.path, db, dry_run=args.dry_run)
            elif args.source == "signal":
                summary = import_signal_desktop(args.path, db, dry_run=args.dry_run)
            elif args.source == "whatsapp":
                summary = import_whatsapp_chatstorage(args.path, db, dry_run=args.dry_run)
            else:
                raise AssertionError(args.source)
        except Exception as exc:
            print(f"context-import: {exc}", file=sys.stderr)
            return 1
        if args.json or args.verbose:
            print(_json(summary.as_dict()))
        return 0 if summary.status in {"ok", "blocked_decryption_needed"} else 1
    if args.cmd == "context-scan":
        registry = default_registry(ingress_dir=args.ingress_dir)
        try:
            names = registry.names() if args.all_sources else tuple(args.source or ())
            unknown = sorted(set(names) - set(registry.names()))
            if unknown:
                raise ValueError("unknown_source")
            runner = SourceRunner(registry, source_db=args.source_db, inbox_db=args.db or ContextInbox().db_path) if args.db else SourceRunner(registry, source_db=args.source_db)
            results = runner.scan(names, full=args.full, dry_run=args.dry_run, limit=args.limit)
        except Exception as exc:
            print(f"context-scan: {_safe_cli_error(exc)}", file=sys.stderr)
            return 2
        public = {"sources": [result.as_dict() for result in results], "dry_run": args.dry_run}
        if args.json or args.verbose:
            print(_json(public))
        elif results:
            print(" ".join(f"{r.source}={r.status.value}:{r.reason_code}" for r in results))
        return 1 if any(result.status is SourceStatus.ERROR for result in results) else 0
    if args.cmd == "context-source-health":
        registry = default_registry(ingress_dir=args.ingress_dir)
        try:
            names = tuple(args.source or registry.names())
            data = SourceRunner(registry).probe(names, persist=False)
        except Exception as exc:
            print(f"context-source-health: {_safe_cli_error(exc)}", file=sys.stderr)
            return 2
        if args.json:
            print(_json({"sources": data}))
        else:
            print(" ".join(f"{item['source']}={item['status']}:{item['reason_code']}" for item in data))
        return 0
    if args.cmd == "context-retention":
        try:
            data = SourceStore.retention_preview(args.source_db) if args.dry_run else SourceStore(args.source_db).retention()
        except Exception as exc:
            print(f"context-retention: {_safe_cli_error(exc)}", file=sys.stderr)
            return 1
        if args.json or args.verbose:
            print(_json(data))
        else:
            print(" ".join(f"{key}={value}" for key, value in data.items()))
        return 0
    if args.cmd == "context-import-export":
        try:
            adapter = SafeImportAdapter(args.source, args.path, args.format)
            registry = SourceRegistry([adapter])
            runner = SourceRunner(registry, source_db=args.source_db, inbox_db=args.db or ContextInbox().db_path) if args.db else SourceRunner(registry, source_db=args.source_db)
            result = runner.scan_one(args.source, full=True, dry_run=args.dry_run, limit=args.limit)
        except Exception as exc:
            print(f"context-import-export: {_safe_cli_error(exc)}", file=sys.stderr)
            return 2
        if args.json or args.verbose:
            print(_json(result.as_dict()))
        else:
            print(f"{result.source}={result.status.value}:{result.reason_code} imported={result.stored}")
        return 1 if result.status is SourceStatus.ERROR else 0
    if args.cmd == "context-rank":
        db = db_from_args(args, load_manifest)
        ranked = ContextInbox(db).rank_all()
        data = {"ranked": len(ranked), "events": [r.__dict__ for r in ranked]}
        if args.json or args.verbose:
            print(_json(data))
        return 0
    if args.cmd == "context-brief":
        db = db_from_args(args, load_manifest)
        brief = ContextInbox(db).brief()
        if args.json:
            print(_json(brief))
        else:
            for tier in ("immediate", "briefing", "archive"):
                print(f"{tier}: {len(brief[tier])}")
                if args.verbose:
                    for item in brief[tier]:
                        print(f"- [{item['score']}] {item['conversation']} {item['sender']}: {item['body']}")
        return 0
    if args.cmd == "context-export-openviking":
        db = db_from_args(args, load_manifest)
        inbox = ContextInbox(db)
        if args.dry_run:
            count = sum(len(v) for k, v in inbox.brief().items() if k in {"immediate", "briefing"})
        else:
            count = inbox.export_openviking(args.output)
        if args.json or args.verbose:
            print(_json({"exported": count, "output": str(args.output), "dry_run": args.dry_run}))
        return 0
    if args.cmd == "context-alerts":
        db = db_from_args(args, load_manifest)
        inbox = ContextInbox(db)
        result = inbox.claim_alerts(initialize=args.initialize, prepared=args.prepared)
        if args.json:
            public_result = {key: value for key, value in result.items() if key != "owner"}
            print(_json(public_result))
            sys.stdout.flush()
        elif not args.initialize:
            text = "\n\n".join(item["text"] for item in result["alerts"])
            if text:
                print(text)
                sys.stdout.flush()
        if not args.initialize and result.get("alerts"):
            inbox.mark_alerts_emitted((item["fingerprint"] for item in result["alerts"]), str(result.get("owner") or ""))
        return 0
    if args.cmd == "context-feedback":
        db = db_from_args(args, load_manifest)
        try:
            result = ContextInbox(db).set_reminder_status(args.reminder, args.status)
        except KeyError:
            print(f"context-feedback: reminder not found: {args.reminder}", file=sys.stderr)
            return 1
        if args.json or args.verbose:
            print(_json(result))
        return 0
    if args.cmd == "context-daily-brief":
        preferences_path = args.preferences or default_preferences_path()
        try:
            preferences_value = load_preferences(preferences_path)
            event_limit = args.event_limit if args.event_limit is not None else preferences_value.event_limit
            reminder_limit = args.reminder_limit if args.reminder_limit is not None else preferences_value.reminder_limit
            habit_limit = args.habit_limit if args.habit_limit is not None else preferences_value.habit_limit
            max_output_characters = (
                args.max_output_characters
                if args.max_output_characters is not None
                else preferences_value.max_output_characters
            )
            included_sections = (
                tuple(args.include_section)
                if args.include_section is not None
                else preferences_value.included_sections
            )
            excluded_platforms = (
                tuple(args.exclude_platform)
                if args.exclude_platform is not None
                else preferences_value.excluded_platforms
            )
            db = db_from_args(args, load_manifest)
            inbox = ContextInbox(db)
            brief = inbox.daily_brief(
                hours=args.hours,
                event_limit=event_limit,
                reminder_limit=reminder_limit,
                habit_limit=habit_limit,
                included_sections=included_sections,
                excluded_platforms=excluded_platforms,
                quiet_when_empty=preferences_value.quiet_when_empty,
            )
            text = inbox.format_daily_brief(brief, max_output_characters=max_output_characters)
        except Exception as exc:
            print(f"context-daily-brief: {_safe_cli_error(exc)}", file=sys.stderr)
            return 2
        claim_owner = None if args.dry_run else inbox.claim_daily_brief(brief, force=args.force)
        claimed = bool(claim_owner)
        wrote_output = False
        if args.json:
            print(
                _json(
                    {
                        **brief,
                        "brief": text,
                        "claimed": claimed,
                        "dry_run": args.dry_run,
                        "force": args.force,
                        "max_output_characters": max_output_characters,
                        "preferences_path": str(Path(preferences_path).expanduser()),
                    }
                )
            )
            sys.stdout.flush()
            wrote_output = True
        elif args.dry_run or claimed:
            if text:
                print(text)
                sys.stdout.flush()
                wrote_output = True
        if wrote_output and claimed and not args.force and not args.dry_run:
            inbox.mark_daily_brief_emitted(str(claim_owner or ""))
        return 0
    if args.cmd == "context-stats":
        db = db_from_args(args, load_manifest)
        stats = ContextInbox(db).stats()
        if args.json:
            print(_json(stats))
        else:
            print(stats)
        return 0
    raise AssertionError(args.cmd)


def context_alerts_main(argv: list[str] | None = None) -> int:
    return main(["context-alerts", *(argv if argv is not None else sys.argv[1:])])


def context_daily_brief_main(argv: list[str] | None = None) -> int:
    return main(["context-daily-brief", *(argv if argv is not None else sys.argv[1:])])


def context_feedback_main(argv: list[str] | None = None) -> int:
    return main(["context-feedback", *(argv if argv is not None else sys.argv[1:])])


def _json(data: object) -> str:
    import json

    return json.dumps(data, sort_keys=True)


def _metadata_projection(data: dict[str, object], keys: tuple[str, ...]) -> dict[str, object]:
    """Return an explicit metadata allow-list for operator-facing CLI output."""

    return {key: data[key] for key in keys if key in data}


def _public_intent(data: dict[str, object]) -> dict[str, object]:
    return _metadata_projection(
        data,
        (
            "intent_id",
            "state",
            "source",
            "provenance_hash",
            "requires_approval",
            "created_at",
            "updated_at",
            "terminal_at",
            "active",
            "created",
        ),
    )


def _public_job(data: dict[str, object]) -> dict[str, object]:
    return _metadata_projection(
        data,
        (
            "job_id",
            "intent_id",
            "job_kind",
            "state",
            "attempts",
            "max_attempts",
            "lease_until",
            "lease_generation",
            "heartbeat_at",
            "created_at",
            "updated_at",
            "terminal_at",
            "active",
            "created",
        ),
    )


def _public_improvement(data: dict[str, object]) -> dict[str, object]:
    return _metadata_projection(
        data,
        (
            "proposal_id",
            "observation_count",
            "evidence_count",
            "counterevidence_count",
            "risk_class",
            "target_kind",
            "expected_metric",
            "state",
            "canary_state",
            "created_at",
            "updated_at",
            "terminal_at",
            "created",
        ),
    )


def _safe_cli_error(exc: BaseException) -> str:
    return redact(" ".join(str(exc).split()))[:500] or type(exc).__name__
