"""
main.py
=======
Master orchestrator for the Kommo CRM Milestone 1 extraction pipeline.

ORCHESTRATION FLOW
──────────────────
  Step 1  → Validate token (file exists, JSON readable)
  Step 2  → Refresh token if expired (or within 5-min buffer)
  Step 3  → Fetch pipelines + stages  → outputs/pipelines.json
  Step 4  → Fetch leads               → outputs/leads.json
  Step 5  → Fetch tasks               → outputs/tasks.json
  Step 6  → Save all JSON outputs (atomic writes, dead-letter on failure)
  Step 7  → Print structured summary log

USAGE
─────
    # Activate venv first
    source .venv/bin/activate

    # Full extraction
    python main.py

    # Incremental — last 24 hours
    python main.py --since 86400

    # Slim tasks (6-field compact output)
    python main.py --slim-tasks

    # Debug HTTP traffic
    python main.py --debug

EXIT CODES
──────────
    0 — All steps succeeded
    1 — One or more extractors failed (check logs)
    2 — Auth / config failure (nothing ran)

SCALABILITY NOTES
─────────────────
  - Add new entities by registering a new Step in _build_steps()
  - Steps are independent — failure in one never blocks the others
    (unless --fail-fast is set)
  - Incremental filtering is applied uniformly across all entities
    via shared since_ts parameter
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Load environment variables before any module that reads them
from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from auth.oauth import (
    KommoOAuthClient,
    KommoOAuthError,
    KommoTokenMissingError,
    KommoAuthorizationError,
)
from api.client import KommoAPIClient, KommoClientError
from api.leads import LeadsExtractor, ExtractionResult
from api.pipelines import PipelinesExtractor, PipelineExtractionResult
from api.tasks import TasksExtractor, TaskExtractionResult
from utils.state_manager import StateManager
from utils.logger import configure_logging, get_logger
from utils.retry import retry_api_call
from utils.exceptions import KommoConfigError

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _configure_logging(debug: bool = False) -> logging.Logger:
    """
    Configure root logger using the shared utils.logger utility.
    Activates: rotating JSON file (logs/kommo.log) + coloured console + error log.
    """
    level = "DEBUG" if debug else os.environ.get("LOG_LEVEL", "INFO")
    configure_logging(log_level=level, log_dir="logs")
    return get_logger(__name__)


# ---------------------------------------------------------------------------
# Step result dataclass
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """
    Outcome of a single extraction step.

    Attributes:
        name:       Human-readable step name (e.g. "Leads").
        success:    True if the step completed without a fatal error.
        records:    Number of records successfully extracted.
        skipped:    Number of records that failed validation (dead-letter).
        output_path: Path to the written JSON file (None if nothing extracted).
        error:      Exception message if the step failed.
        duration_s: Elapsed time for this step in seconds.
    """
    name:        str
    success:     bool       = True
    records:     int        = 0
    skipped:     int        = 0
    output_path: Path | None = None
    error:       str | None = None
    duration_s:  float      = 0.0

    @property
    def status_icon(self) -> str:
        if not self.success:
            return "❌"
        if self.skipped:
            return "⚠️ "
        return "✅"


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Kommo CRM — Full Extraction Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    p.add_argument(
        "--since", type=int, metavar="SECONDS",
        help="Incremental mode: only extract records updated in last N seconds "
             "(e.g. --since 86400 for last 24h)",
    )
    p.add_argument(
        "--since-ts", type=int, metavar="UNIX_TS", dest="since_ts",
        help="Incremental mode: only extract records updated at or after this Unix timestamp",
    )
    p.add_argument(
        "--output-dir", type=Path, default=Path("outputs"),
        dest="output_dir", metavar="PATH",
        help="Directory for output JSON files (default: outputs/)",
    )
    p.add_argument(
        "--slim-tasks", action="store_true", dest="slim_tasks",
        help="Also write tasks_slim.json with 6 core fields (id, text, entity_id, "
             "due_date, is_completed, responsible_user_id)",
    )
    p.add_argument(
        "--auto-incremental", action="store_true", dest="auto_incremental",
        help="Auto-detect last run timestamp from state/sync_state.json "
             "and run incrementally (overrides --since / --since-ts)",
    )
    p.add_argument(
        "--fail-fast", action="store_true", dest="fail_fast",
        help="Abort the entire run on the first extractor failure",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG logging (includes raw HTTP request/response details)",
    )
    return p


# ---------------------------------------------------------------------------
# Core orchestration steps
# ---------------------------------------------------------------------------

def step1_validate_token(oauth: KommoOAuthClient, log: logging.Logger) -> None:
    """
    Step 1 — Validate that a token file exists and is readable.

    Raises:
        KommoTokenMissingError: Token file missing (run_auth.py not yet run).
        KommoOAuthError:        Token file corrupt or unreadable.
    """
    log.info("[Step 1/7] Validating token store...")

    if not oauth.tokens_exist():
        raise KommoTokenMissingError(
            "Token store not found. "
            "Run `python run_auth.py` to complete OAuth authorization first."
        )

    # Load and parse to catch corruption early
    info = oauth.token_info()

    log.info(
        "Token validated — account=%s, expires_in=%.0fs, is_expired=%s",
        info.get("account_domain"),
        info.get("seconds_until_expiry", 0),
        info.get("is_expired"),
    )


def step2_refresh_token_if_needed(oauth: KommoOAuthClient, log: logging.Logger) -> None:
    """
    Step 2 — Refresh the access token if it has expired or is near expiry.

    get_valid_token() handles the check + refresh atomically.
    We call it here explicitly so any auth failures surface before API calls start.

    Raises:
        KommoAuthorizationError: Refresh token rejected (re-auth needed).
        KommoOAuthError:         Other auth failure.
    """
    log.info("[Step 2/7] Checking token freshness...")

    token = oauth.get_valid_token()    # Refreshes internally if needed
    log.info("Access token is valid and ready (length=%d)", len(token))


def step3_fetch_pipelines(
    client: KommoAPIClient,
    output_dir: Path,
    log: logging.Logger,
) -> StepResult:
    """
    Step 3 — Fetch all pipelines and their stages.

    No pagination needed (rarely > 20 pipelines per account).
    Stages are embedded in the pipeline response.

    Returns:
        StepResult with pipeline + stage counts.
    """
    log.info("[Step 3/7] Fetching pipelines and stages...")
    started = time.monotonic()

    try:
        extractor = PipelinesExtractor(client=client, output_dir=output_dir)
        result = extractor.extract_all()

        log.info(
            "Pipelines complete — pipelines=%d, stages=%d, duration=%.1fs",
            result.total_pipelines,
            result.total_stages,
            result.duration_seconds,
        )

        return StepResult(
            name="Pipelines",
            success=True,
            records=result.total_pipelines,
            skipped=result.failed_pipelines,
            output_path=result.output_path,
            duration_s=time.monotonic() - started,
        )

    except KommoClientError as exc:
        log.error("Pipeline extraction failed: %s", exc)
        return StepResult(
            name="Pipelines",
            success=False,
            error=str(exc),
            duration_s=time.monotonic() - started,
        )


def step4_fetch_leads(
    client: KommoAPIClient,
    output_dir: Path,
    since_ts: int | None,
    log: logging.Logger,
) -> StepResult:
    """
    Step 4 — Fetch all leads with full pagination.

    Supports incremental mode via since_ts (updated_at filter).

    Args:
        since_ts: Optional Unix timestamp — only fetch leads updated after this.

    Returns:
        StepResult with lead count and output path.
    """
    mode = f"since_ts={since_ts}" if since_ts else "full"
    log.info("[Step 4/7] Fetching leads (%s mode)...", mode)
    started = time.monotonic()

    try:
        extractor = LeadsExtractor(client=client, output_dir=output_dir)

        result: ExtractionResult = (
            extractor.extract_updated_since(since_ts)
            if since_ts
            else extractor.extract_all()
        )

        log.info(
            "Leads complete — records=%d, pages=%d, failed=%d, duration=%.1fs",
            result.total_records,
            result.pages_fetched,
            result.failed_records,
            result.duration_seconds,
        )

        return StepResult(
            name="Leads",
            success=True,
            records=result.total_records,
            skipped=result.failed_records,
            output_path=result.output_path,
            duration_s=time.monotonic() - started,
        )

    except KommoClientError as exc:
        log.error("Lead extraction failed: %s", exc)
        return StepResult(
            name="Leads",
            success=False,
            error=str(exc),
            duration_s=time.monotonic() - started,
        )


def step5_fetch_tasks(
    client: KommoAPIClient,
    output_dir: Path,
    since_ts: int | None,
    slim: bool,
    log: logging.Logger,
) -> StepResult:
    """
    Step 5 — Fetch all tasks with full pagination.

    Args:
        since_ts: Optional Unix timestamp for incremental extraction.
        slim:     If True, also write tasks_slim.json (6 core fields).

    Returns:
        StepResult with task count, overdue count, and output paths.
    """
    mode = f"since_ts={since_ts}" if since_ts else "full"
    log.info("[Step 5/7] Fetching tasks (%s mode, slim=%s)...", mode, slim)
    started = time.monotonic()

    try:
        extractor = TasksExtractor(client=client, output_dir=output_dir)
        slim_path: Path | None = None

        if slim:
            result_t, slim_path = extractor.extract_slim()
        elif since_ts:
            result_t = extractor.extract_updated_since(since_ts)
        else:
            result_t = extractor.extract_all()

        log.info(
            "Tasks complete — records=%d, completed=%d, overdue=%d, "
            "failed=%d, duration=%.1fs",
            result_t.total_records,
            result_t.completed_count,
            result_t.overdue_count,
            result_t.failed_records,
            result_t.duration_seconds,
        )

        if slim_path:
            log.info("Slim task output written → %s", slim_path)

        return StepResult(
            name="Tasks",
            success=True,
            records=result_t.total_records,
            skipped=result_t.failed_records,
            output_path=result_t.output_path,
            duration_s=time.monotonic() - started,
        )

    except KommoClientError as exc:
        log.error("Task extraction failed: %s", exc)
        return StepResult(
            name="Tasks",
            success=False,
            error=str(exc),
            duration_s=time.monotonic() - started,
        )


def step6_verify_outputs(steps: list[StepResult], log: logging.Logger) -> None:
    """
    Step 6 — Verify all expected JSON output files exist and are non-empty.

    Logs a warning for any output file that is missing or zero bytes.
    Does NOT raise — missing files are surfaced in the summary (Step 7).
    """
    log.info("[Step 6/7] Verifying output files...")

    for step in steps:
        if not step.success:
            log.warning("  ⚠  %s — skipped (step failed)", step.name)
            continue

        if step.output_path is None:
            log.warning("  ⚠  %s — no output file written (0 records?)", step.name)
            continue

        if not step.output_path.exists():
            log.error("  ✗  %s — output file missing: %s", step.name, step.output_path)
            continue

        size_bytes = step.output_path.stat().st_size
        log.info(
            "  ✓  %s → %s (%.1f KB)",
            step.name,
            step.output_path,
            size_bytes / 1024,
        )


def step7_print_summary(
    steps: list[StepResult],
    run_start: float,
    since_ts: int | None,
    log: logging.Logger,
) -> None:
    """
    Step 7 — Print a structured human-readable and machine-readable summary.

    Logs the full summary at INFO level (appears in log files).
    Also prints a formatted console report.
    """
    total_duration = time.monotonic() - run_start
    total_records  = sum(s.records for s in steps)
    any_failed     = any(not s.success for s in steps)
    any_warnings   = any(s.skipped > 0 for s in steps if s.success)

    run_status = "FAILED" if any_failed else ("WARNINGS" if any_warnings else "SUCCESS")

    log.info("[Step 7/7] Run complete — status=%s, total_records=%d, duration=%.1fs",
             run_status, total_records, total_duration)

    # Structured log record for monitoring / alerting systems
    log.info(
        "EXTRACTION_SUMMARY status=%s records=%d duration_s=%.1f "
        "incremental=%s pipelines=%s leads=%s tasks=%s",
        run_status,
        total_records,
        total_duration,
        bool(since_ts),
        next((s.records for s in steps if s.name == "Pipelines"), 0),
        next((s.records for s in steps if s.name == "Leads"), 0),
        next((s.records for s in steps if s.name == "Tasks"), 0),
    )

    # Console summary table
    width = 65
    print("\n" + "═" * width)
    if any_failed:
        print("  ⚠️  EXTRACTION COMPLETE — WITH ERRORS")
    else:
        print("  ✅  EXTRACTION COMPLETE")
    print("═" * width)

    for step in steps:
        icon = step.status_icon
        dur  = f"{step.duration_s:.1f}s"

        if not step.success:
            print(f"  {icon}  {step.name:<14} FAILED — {step.error}")
        else:
            rec_str = f"{step.records:,} records"
            if step.name == "Pipelines":
                # For pipelines, record count = pipelines (not stages)
                rec_str = f"{step.records} pipelines"
            print(f"  {icon}  {step.name:<14} {rec_str:<20} [{dur}]")

            if step.output_path and step.output_path.exists():
                kb = step.output_path.stat().st_size / 1024
                print(f"            📄 {step.output_path}  ({kb:.1f} KB)")

            if step.skipped:
                print(f"            ⚠️  {step.skipped} records failed validation → outputs/errors/")

    print("─" * width)
    mode_label = f"incremental (since_ts={since_ts})" if since_ts else "full"
    print(f"  Mode          : {mode_label}")
    print(f"  Total records : {total_records:,}")
    print(f"  Total duration: {total_duration:.1f}s")
    print(f"  Timestamp     : {datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("═" * width + "\n")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Orchestrate the full Kommo CRM extraction pipeline.

    Returns:
        Exit code: 0=success, 1=extraction errors, 2=auth/config errors.
    """
    args = _build_parser().parse_args()
    log  = _configure_logging(debug=args.debug)

    # ------------------------------------------------------------------
    # Load and validate config (fail fast with a clear message)
    # ------------------------------------------------------------------
    try:
        from config import settings  # noqa: F401 — validates .env at import
    except KommoConfigError as exc:
        print(f"\n  ❌  Configuration error:\n", file=sys.stderr)
        print(f"     {exc}", file=sys.stderr)
        print("\n  → Copy .env.example to .env and fill in all required values.\n",
              file=sys.stderr)
        return 2

    run_start = time.monotonic()
    run_ts    = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------
    # Banner
    # ------------------------------------------------------------------
    print("\n" + "═" * 65)
    print("  Kommo CRM Integration — Extraction Pipeline")
    print(f"  Started : {run_ts}")
    print("═" * 65 + "\n")

    # ------------------------------------------------------------------
    # Initialise state manager
    # ------------------------------------------------------------------
    sm = StateManager()
    log.info("Sync state loaded from %s", sm._path)

    # ------------------------------------------------------------------
    # Resolve incremental timestamp
    # ------------------------------------------------------------------
    since_ts: int | None = None
    if getattr(args, "auto_incremental", False):
        # Use the oldest last_run_at across all entities so nothing is missed
        candidates = [
            sm.get_last_run_timestamp(e)
            for e in ("leads", "tasks", "pipelines")
        ]
        valid = [ts for ts in candidates if ts is not None]
        if valid:
            since_ts = min(valid)   # oldest = safest
            log.info("Auto-incremental: since_ts=%d (oldest entity high-water mark)", since_ts)
        else:
            log.info("Auto-incremental: no prior state found — running full extraction")
    elif args.since_ts:
        since_ts = args.since_ts
        log.info("Mode: incremental — since Unix timestamp %d", since_ts)
    elif args.since:
        since_ts = int(time.time()) - args.since
        log.info("Mode: incremental — last %ds (since_ts=%d)", args.since, since_ts)
    else:
        log.info("Mode: full extraction")

    # ------------------------------------------------------------------
    # Initialise OAuth client (validates all required env vars)
    # ------------------------------------------------------------------
    try:
        oauth = KommoOAuthClient()
    except EnvironmentError as exc:
        log.critical("Configuration error: %s", exc)
        print(f"\n  ❌  Config error: {exc}")
        print("      → Check your .env file. See .env.example for required variables.\n")
        return 2

    # ------------------------------------------------------------------
    # STEP 1: Validate token
    # ------------------------------------------------------------------
    try:
        step1_validate_token(oauth, log)
    except KommoTokenMissingError as exc:
        log.critical("Token missing: %s", exc)
        print(f"\n  ❌  {exc}")
        print("      → Run `python run_auth.py` to authorize first.\n")
        return 2
    except KommoOAuthError as exc:
        log.critical("Token validation error: %s", exc)
        print(f"\n  ❌  Token error: {exc}\n")
        return 2

    # ------------------------------------------------------------------
    # STEP 2: Refresh token if needed
    # ------------------------------------------------------------------
    try:
        step2_refresh_token_if_needed(oauth, log)
    except KommoAuthorizationError as exc:
        log.critical("Token refresh failed — re-authorization needed: %s", exc)
        print(f"\n  ❌  Auth error: {exc}")
        print("      → Run `python run_auth.py` to re-authorize.\n")
        return 2
    except KommoOAuthError as exc:
        log.critical("Unexpected auth error: %s", exc)
        print(f"\n  ❌  Auth error: {exc}\n")
        return 2

    # ------------------------------------------------------------------
    # STEPS 3–5: Run extractors inside shared HTTP session
    # ------------------------------------------------------------------
    step_results: list[StepResult] = []

    try:
        with KommoAPIClient(oauth) as client:

            # Connectivity check
            try:
                account = client.health_check()
                log.info(
                    "Connected — account=%s, domain=%s.kommo.com",
                    account.get("name", "unknown"),
                    oauth.account_domain,
                )
                print(f"  🔗 Connected: {account.get('name')} ({oauth.account_domain}.kommo.com)\n")
            except KommoClientError as exc:
                log.critical("API health check failed: %s", exc)
                print(f"\n  ❌  Cannot reach Kommo API: {exc}")
                print("      → Check KOMMO_ACCOUNT_DOMAIN and network connection.\n")
                return 2

            # ── Step 3: Pipelines ──────────────────────────────────────
            r3 = step3_fetch_pipelines(client, args.output_dir, log)
            step_results.append(r3)
            if r3.success:
                sm.mark_success("pipelines", records=r3.records)
            else:
                sm.mark_failed("pipelines", error=r3.error or "unknown")
            if not r3.success and args.fail_fast:
                log.warning("--fail-fast: aborting after pipeline failure")
                return 1

            # ── Step 4: Leads ──────────────────────────────────────────
            r4 = step4_fetch_leads(client, args.output_dir, since_ts, log)
            step_results.append(r4)
            if r4.success:
                if r4.skipped:
                    sm.mark_partial("leads", records=r4.records, failed_records=r4.skipped)
                else:
                    sm.mark_success("leads", records=r4.records)
            else:
                sm.mark_failed("leads", error=r4.error or "unknown")
            if not r4.success and args.fail_fast:
                log.warning("--fail-fast: aborting after lead failure")
                return 1

            # ── Step 5: Tasks ──────────────────────────────────────────
            r5 = step5_fetch_tasks(
                client, args.output_dir, since_ts,
                slim=args.slim_tasks, log=log,
            )
            step_results.append(r5)
            if r5.success:
                if r5.skipped:
                    sm.mark_partial("tasks", records=r5.records, failed_records=r5.skipped)
                else:
                    sm.mark_success("tasks", records=r5.records)
            else:
                sm.mark_failed("tasks", error=r5.error or "unknown")
            if not r5.success and args.fail_fast:
                log.warning("--fail-fast: aborting after task failure")
                return 1

    except Exception as exc:
        log.exception("Unexpected fatal error during extraction: %s", exc)
        print(f"\n  ❌  Unexpected error: {exc}\n")
        return 1

    # ------------------------------------------------------------------
    # STEP 6: Verify outputs
    # ------------------------------------------------------------------
    step6_verify_outputs(step_results, log)

    # ------------------------------------------------------------------
    # STEP 7: Print summary + sync state
    # ------------------------------------------------------------------
    step7_print_summary(step_results, run_start, since_ts, log)

    # Print sync state so user sees the updated high-water marks
    log.info("Updated sync state:\n%s", sm.summary())

    # Exit code: 1 if any step failed, 0 otherwise
    return 1 if any(not s.success for s in step_results) else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
