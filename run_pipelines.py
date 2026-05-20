"""
run_pipelines.py
================
Standalone runner for Kommo pipeline and stage extraction.

USAGE
─────
    python run_pipelines.py
    python run_pipelines.py --output-dir /data/kommo
    python run_pipelines.py --debug
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from auth.oauth import KommoOAuthClient, KommoOAuthError
from api.client import KommoAPIClient, KommoClientError
from api.pipelines import PipelinesExtractor, PipelineExtractionResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Extract Kommo pipelines and stages to JSON")
    p.add_argument("--output-dir", type=Path, default=Path("outputs"), dest="output_dir")
    p.add_argument("--debug", action="store_true", help="Enable DEBUG logging")
    return p


def main() -> int:
    args = _build_parser().parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    print("\n" + "=" * 60)
    print("  Kommo Pipeline & Stage Extraction")
    print("=" * 60 + "\n")

    # ------------------------------------------------------------------
    # 1. Initialise OAuth
    # ------------------------------------------------------------------
    try:
        oauth = KommoOAuthClient()
    except EnvironmentError as exc:
        print(f"\n❌  Config error: {exc}\n")
        return 1

    if not oauth.tokens_exist():
        print("\n❌  No tokens found. Run `python run_auth.py` first.\n")
        return 1

    # ------------------------------------------------------------------
    # 2. Extract
    # ------------------------------------------------------------------
    result: PipelineExtractionResult | None = None

    try:
        with KommoAPIClient(oauth) as client:

            try:
                account = client.health_check()
                logger.info("Connected: %s", account.get("name", "unknown"))
            except KommoClientError as exc:
                print(f"\n❌  API connectivity failed: {exc}\n")
                return 1

            extractor = PipelinesExtractor(client, output_dir=args.output_dir)
            result = extractor.extract_all()

    except KommoOAuthError as exc:
        print(f"\n❌  Auth error: {exc}\n")
        return 1
    except KommoClientError as exc:
        print(f"\n❌  API error: {exc}\n")
        logger.exception("Unrecoverable API error")
        return 1
    except Exception as exc:
        print(f"\n❌  Unexpected error: {exc}\n")
        logger.exception("Unexpected error")
        return 1

    # ------------------------------------------------------------------
    # 3. Print summary
    # ------------------------------------------------------------------
    print("─" * 60)
    print("  Extraction Complete")
    print("─" * 60)
    print(f"  ✅  Pipelines   : {result.total_pipelines}")
    print(f"  ✅  Total stages: {result.total_stages}")
    print(f"  ⏱️   Duration    : {result.duration_seconds:.2f}s")

    if result.output_path:
        kb = result.output_path.stat().st_size / 1024
        print(f"  💾  Output      : {result.output_path}  ({kb:.1f} KB)")

    if result.failed_pipelines:
        print(f"  ⚠️   Failed      : {result.failed_pipelines} → {result.dead_letter_path}")
    else:
        print(f"  ✓   Validation  : All records passed")

    print("─" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
