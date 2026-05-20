"""
run_extraction.py
=================
Master orchestrator for Kommo CRM Milestone 1 extraction pipeline.

Runs all three extractors in dependency order:
  1. Pipelines + Stages  (no deps — reference data)
  2. Leads               (references pipeline_id + status_id)
  3. Tasks               (references entity_id on leads/contacts)

Each extractor is independent — a failure in one does NOT stop the others
unless --fail-fast is specified. The final exit code reflects the worst
outcome across all extractors.

USAGE
─────
    # Full extraction — all entities
    python run_extraction.py

    # Slim tasks output (6-field JSON alongside full)
    python run_extraction.py --slim-tasks

    # Only open (incomplete) tasks
    python run_extraction.py --open-tasks-only

    # Incremental — records updated in last N seconds
    python run_extraction.py --since 86400

    # Custom output directory
    python run_extraction.py --output-dir /data/kommo

    # Stop on first extractor failure
    python run_extraction.py --fail-fast

    # Verbose HTTP-level logging
    python run_extraction.py --debug

EXIT CODES
──────────
    0 — All extractors completed (validation errors → dead-letter, not exit 1)
    1 — One or more extractors failed fatally
    2 — Configuration / authentication error (nothing ran)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
load_dotenv()

from auth.oauth import KommoOAuthClient, KommoOAuthError
from api.client import KommoAPIClient, KommoClientError
from api.leads import LeadsExtractor, ExtractionResult
from api.pipelines import PipelinesExtractor, PipelineExtractionResult
from api.tasks import TasksExtractor, TaskExtractionResult

# ---------------------------------------------------------------------------
# Logging — structured, human-readable for CLI
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Run Summary
# ---------------------------------------------------------------------------

@dataclass
class RunSummary:
    """Aggregated result across all extractors for the final report."""

    run_id:     str  = field(default_factory=lambda: f"run_{int(time.time())}")
    started_at: str  = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())
    finished_at: str | None = None

    pipelines_result: PipelineExtractionResult | None = None
    leads_result:     ExtractionResult | None          = None
    tasks_result:     TaskExtractionResult | None      = None

    pipelines_error: str | None = None
    leads_error:     str | None = None
    tasks_error:     str | None = None

    @property
    def has_fatal_errors(self) -> bool:
        return any([self.pipelines_error, self.leads_error, self.tasks_error])

    @property
    def total_records(self) -> int:
        total = 0
        if self.pipelines_result:
            total += self.pipelines_result.total_pipelines
        if self.leads_result:
            total += self.leads_result.total_records
        if self.tasks_result:
            total += self.tasks_result.total_records
        return total

    @property
    def total_duration(self) -> float:
        durations = []
        for r in [self.pipelines_result, self.leads_result, self.tasks_result]:
            if r:
                durations.append(r.duration_seconds)
        return sum(durations)


# ---------------------------------------------------------------------------
# Argument Parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Kommo CRM Milestone 1 — Full Extraction Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Extraction scope
    g = p.add_argument_group("Extraction scope")
    g.add_argument(
        "--skip-pipelines", action="store_true", dest="skip_pipelines",
        help="Skip pipeline/stage extraction",
    )
    g.add_argument(
        "--skip-leads", action="store_true", dest="skip_leads",
        help="Skip lead extraction",
    )
    g.add_argument(
        "--skip-tasks", action="store_true", dest="skip_tasks",
        help="Skip task extraction",
    )
    g.add_argument(
        "--slim-tasks", action="store_true", dest="slim_tasks",
        help="Also write tasks_slim.json (6 core fields only)",
    )
    g.add_argument(
        "--open-tasks-only", action="store_true", dest="open_tasks_only",
        help="Extract only incomplete (open) tasks",
    )

    # Incremental
    g2 = p.add_argument_group("Incremental extraction")
    g2.add_argument(
        "--since", type=int, metavar="SECONDS",
        help="Only extract records updated in the last N seconds",
    )
    g2.add_argument(
        "--since-ts", type=int, metavar="UNIX_TS", dest="since_ts",
        help="Only extract records updated at or after this Unix timestamp",
    )

    # Output
    g3 = p.add_argument_group("Output")
    g3.add_argument(
        "--output-dir", type=Path, default=Path("outputs"),
        dest="output_dir", metavar="PATH",
        help="Output directory (default: outputs/)",
    )

    # Behaviour
    g4 = p.add_argument_group("Behaviour")
    g4.add_argument(
        "--fail-fast", action="store_true", dest="fail_fast",
        help="Stop immediately if any extractor fails",
    )
    g4.add_argument(
        "--debug", action="store_true",
        help="Enable DEBUG-level logging (verbose HTTP logs)",
    )

    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = _build_parser().parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    summary = RunSummary()

    _print_banner(summary.run_id)

    # ------------------------------------------------------------------
    # 1. Resolve incremental timestamp
    # ------------------------------------------------------------------
    since_ts: int | None = None
    if args.since_ts:
        since_ts = args.since_ts
        logger.info("Incremental mode: updated_at >= %d", since_ts)
    elif args.since:
        since_ts = int(time.time()) - args.since
        logger.info("Incremental mode: last %ds (>= %d)", args.since, since_ts)
    else:
        logger.info("Full extraction mode")

    # ------------------------------------------------------------------
    # 2. Initialise OAuth
    # ------------------------------------------------------------------
    try:
        oauth = KommoOAuthClient()
    except EnvironmentError as exc:
        _print_error(f"Configuration error: {exc}", "Check your .env file.")
        return 2

    if not oauth.tokens_exist():
        _print_error(
            "No token store found.",
            "Run `python run_auth.py` first to complete OAuth authorization.",
        )
        return 2

    try:
        info = oauth.token_info()
        logger.info("Token valid — expires in %.0fs", info["seconds_until_expiry"])
    except KommoOAuthError as exc:
        _print_error(f"Token error: {exc}", "Re-run `python run_auth.py`.")
        return 2

    # ------------------------------------------------------------------
    # 3. Open shared HTTP session + run extractors
    # ------------------------------------------------------------------
    try:
        with KommoAPIClient(oauth) as client:

            # Health check
            try:
                account = client.health_check()
                logger.info(
                    "Connected to Kommo: %s (id=%s)",
                    account.get("name", "unknown"),
                    account.get("id", "?"),
                )
                print(f"  🔗 Account: {account.get('name')} ({oauth.account_domain}.kommo.com)\n")
            except KommoClientError as exc:
                _print_error(f"API connectivity check failed: {exc}")
                return 2

            # ── Extractor 1: Pipelines ─────────────────────────────────
            if not args.skip_pipelines:
                print("  [1/3] Extracting pipelines and stages...")
                try:
                    result = PipelinesExtractor(client, output_dir=args.output_dir).extract_all()
                    summary.pipelines_result = result
                    print(f"        ✅ {result.total_pipelines} pipelines, "
                          f"{result.total_stages} stages — {result.duration_seconds:.1f}s")
                except KommoClientError as exc:
                    summary.pipelines_error = str(exc)
                    logger.error("Pipeline extraction failed: %s", exc)
                    print(f"        ❌ Failed: {exc}")
                    if args.fail_fast:
                        return 1
            else:
                print("  [1/3] Pipelines — SKIPPED\n")

            # ── Extractor 2: Leads ─────────────────────────────────────
            if not args.skip_leads:
                print("\n  [2/3] Extracting leads...")
                try:
                    extractor = LeadsExtractor(client, output_dir=args.output_dir)
                    if since_ts:
                        result_l = extractor.extract_updated_since(since_ts)
                    else:
                        result_l = extractor.extract_all()
                    summary.leads_result = result_l
                    print(f"        ✅ {result_l.total_records:,} leads, "
                          f"{result_l.pages_fetched} pages — {result_l.duration_seconds:.1f}s")
                    if result_l.failed_records:
                        print(f"        ⚠️  {result_l.failed_records} failed → {result_l.dead_letter_path}")
                except KommoClientError as exc:
                    summary.leads_error = str(exc)
                    logger.error("Lead extraction failed: %s", exc)
                    print(f"        ❌ Failed: {exc}")
                    if args.fail_fast:
                        return 1
            else:
                print("\n  [2/3] Leads — SKIPPED")

            # ── Extractor 3: Tasks ─────────────────────────────────────
            if not args.skip_tasks:
                print("\n  [3/3] Extracting tasks...")
                try:
                    task_extractor = TasksExtractor(client, output_dir=args.output_dir)

                    if args.slim_tasks:
                        result_t, slim_path = task_extractor.extract_slim()
                        summary.tasks_result = result_t
                        print(f"        ✅ {result_t.total_records:,} tasks "
                              f"({result_t.completed_count} completed, "
                              f"{result_t.overdue_count} overdue) — {result_t.duration_seconds:.1f}s")
                        print(f"        📄 Slim output → {slim_path}")
                    elif args.open_tasks_only:
                        result_t = task_extractor.extract_open_tasks()
                        summary.tasks_result = result_t
                        print(f"        ✅ {result_t.total_records:,} open tasks — {result_t.duration_seconds:.1f}s")
                    elif since_ts:
                        result_t = task_extractor.extract_updated_since(since_ts)
                        summary.tasks_result = result_t
                        print(f"        ✅ {result_t.total_records:,} tasks — {result_t.duration_seconds:.1f}s")
                    else:
                        result_t = task_extractor.extract_all()
                        summary.tasks_result = result_t
                        print(f"        ✅ {result_t.total_records:,} tasks "
                              f"({result_t.completed_count} completed, "
                              f"{result_t.overdue_count} overdue) — {result_t.duration_seconds:.1f}s")

                    if result_t.failed_records:
                        print(f"        ⚠️  {result_t.failed_records} failed → {result_t.dead_letter_path}")

                except KommoClientError as exc:
                    summary.tasks_error = str(exc)
                    logger.error("Task extraction failed: %s", exc)
                    print(f"        ❌ Failed: {exc}")
                    if args.fail_fast:
                        return 1
            else:
                print("\n  [3/3] Tasks — SKIPPED")

    except KommoOAuthError as exc:
        _print_error(f"Auth error during extraction: {exc}", "Re-run `python run_auth.py`.")
        return 2
    except Exception as exc:
        _print_error(f"Unexpected error: {exc}")
        logger.exception("Unexpected top-level error")
        return 1

    # ------------------------------------------------------------------
    # 4. Final summary
    # ------------------------------------------------------------------
    summary.finished_at = datetime.now(tz=timezone.utc).isoformat()
    _print_run_summary(summary)

    return 1 if summary.has_fatal_errors else 0


# ---------------------------------------------------------------------------
# CLI Formatting
# ---------------------------------------------------------------------------

def _print_banner(run_id: str) -> None:
    print("\n" + "=" * 65)
    print("  Kommo CRM — Milestone 1 Extraction Pipeline")
    print(f"  Run ID: {run_id}")
    print("=" * 65 + "\n")


def _print_error(message: str, hint: str | None = None) -> None:
    print(f"\n  ❌  {message}")
    if hint:
        print(f"      → {hint}")
    print()


def _print_run_summary(s: RunSummary) -> None:
    status = "✅  COMPLETE" if not s.has_fatal_errors else "⚠️  COMPLETE WITH ERRORS"

    print("\n" + "═" * 65)
    print(f"  {status}")
    print("═" * 65)

    # Per-entity rows
    rows = [
        ("Pipelines", s.pipelines_result, s.pipelines_error,
         f"{s.pipelines_result.total_pipelines} pipelines, "
         f"{s.pipelines_result.total_stages} stages" if s.pipelines_result else ""),

        ("Leads",     s.leads_result,     s.leads_error,
         f"{s.leads_result.total_records:,} records" if s.leads_result else ""),

        ("Tasks",     s.tasks_result,     s.tasks_error,
         f"{s.tasks_result.total_records:,} records "
         f"({s.tasks_result.completed_count} done, "
         f"{s.tasks_result.overdue_count} overdue)" if s.tasks_result else ""),
    ]

    for label, result, error, detail in rows:
        if error:
            print(f"  ❌  {label:<12} FAILED — {error}")
        elif result:
            dur = result.duration_seconds
            print(f"  ✅  {label:<12} {detail}  ({dur:.1f}s)")
            if result.output_path:
                kb = result.output_path.stat().st_size / 1024
                print(f"       📄 {result.output_path}  ({kb:.1f} KB)")
        else:
            print(f"  ─   {label:<12} Skipped")

    print()
    print(f"  Total records : {s.total_records:,}")
    print(f"  Total duration: {s.total_duration:.1f}s")
    print(f"  Output dir    : outputs/")
    print("═" * 65 + "\n")


if __name__ == "__main__":
    sys.exit(main())
