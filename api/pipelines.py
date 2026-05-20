"""
api/pipelines.py
================
Pipeline and stage extraction module for the Kommo CRM integration.

KOMMO API BEHAVIOUR
───────────────────
  Endpoint : GET /api/v4/leads/pipelines
  Auth     : Bearer token (injected by KommoAPIClient)

  Response shape:
    {
      "_embedded": {
        "pipelines": [
          {
            "id": 123,
            "name": "Sales Pipeline",
            "sort": 10,
            "is_main": true,
            "_embedded": {
              "statuses": [
                {
                  "id": 111,
                  "name": "New Lead",
                  "sort": 10,
                  "color": "#ffffa8",
                  "type": 0,
                  "pipeline_id": 123
                }
              ]
            }
          }
        ]
      }
    }

  Key facts:
    - No pagination required — accounts rarely exceed 10–20 pipelines.
    - Stages are called "statuses" in the API; "stage" is the UI name.
    - Stage type 0 = regular, 1 = won (closed won), 2 = lost (closed lost).
    - System-generated "won" and "lost" stages are always present.
    - Pipeline sort order determines display order in the Kommo UI.

EXTRACTION FLOW
───────────────
  1. GET /api/v4/leads/pipelines
  2. Extract _embedded.pipelines
  3. For each pipeline → extract _embedded.statuses
  4. Validate both against Pydantic models
  5. Build clean PipelineRecord (with nested StageRecord list)
  6. Save to outputs/pipelines.json

USAGE
─────
    from dotenv import load_dotenv
    load_dotenv()

    from auth.oauth import KommoOAuthClient
    from api.client import KommoAPIClient
    from api.pipelines import PipelinesExtractor

    oauth = KommoOAuthClient()
    with KommoAPIClient(oauth) as client:
        extractor = PipelinesExtractor(client, output_dir="outputs")
        result    = extractor.extract_all()

    print(f"Extracted {result.total_pipelines} pipelines, "
          f"{result.total_stages} stages → {result.output_path}")
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from api.client import KommoAPIClient, KommoClientError, KommoNotFoundError
from utils.logger import get_logger
from utils.retry import retry_api_call

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_KOMMO_API_PATH   = "/leads/pipelines"
_EMBEDDED_PIPELINES = "pipelines"
_EMBEDDED_STATUSES  = "statuses"
_DEFAULT_OUTPUT_DIR = Path("outputs")

# Stage type constants (Kommo internal values)
STAGE_TYPE_REGULAR = 0
STAGE_TYPE_WON     = 1
STAGE_TYPE_LOST    = 2

_STAGE_TYPE_LABELS = {
    STAGE_TYPE_REGULAR: "regular",
    STAGE_TYPE_WON:     "won",
    STAGE_TYPE_LOST:    "lost",
}


# =============================================================================
# Pydantic Models
# =============================================================================

class StageRecord(BaseModel):
    """
    Validated representation of a single Kommo pipeline stage (status).

    In the Kommo API, stages are called "statuses".
    The UI displays them as "stages" — we use "stage" throughout
    for clarity.

    Attributes:
        stage_id:    Kommo internal status ID.
        stage_name:  Display name shown in the Kommo UI.
        pipeline_id: ID of the parent pipeline.
        sort:        Display order (ascending, lower = earlier in funnel).
        color:       Hex colour string (e.g. "#ffffa8") used in UI.
        stage_type:  0=regular, 1=won (success close), 2=lost (failure close).
        stage_type_label: Human-readable type ("regular" / "won" / "lost").
        is_editable: Whether this stage can be modified via the API.
        account_id:  Kommo account this stage belongs to.
    """

    model_config = {"extra": "ignore"}

    # Core fields (aliased from Kommo's "status" naming)
    stage_id:   int       = Field(..., alias="id",          description="Stage (status) ID")
    stage_name: str | None = Field(None, alias="name",     description="Stage display name")
    pipeline_id: int | None = Field(None,                  description="Parent pipeline ID")
    sort:        int | None = Field(None,                  description="Sort order (ascending)")
    color:       str | None = Field(None,                  description="UI hex colour, e.g. #ffffa8")
    stage_type:  int        = Field(default=0, alias="type", description="0=regular, 1=won, 2=lost")
    is_editable: bool       = Field(default=True)
    account_id:  int | None = Field(None)

    # Computed — not from API
    stage_type_label: str = Field(
        default="regular",
        description="Human-readable stage type (regular / won / lost)",
    )

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @model_validator(mode="after")
    def compute_type_label(self) -> "StageRecord":
        """Map numeric stage_type to a human-readable label."""
        self.stage_type_label = _STAGE_TYPE_LABELS.get(self.stage_type, "unknown")
        return self


class PipelineRecord(BaseModel):
    """
    Validated representation of a Kommo pipeline with its embedded stages.

    The output format is designed to be self-contained and human-readable:
    each pipeline carries its full list of stages so downstream consumers
    don't need to join across multiple arrays.

    Attributes:
        pipeline_id:   Kommo internal pipeline ID.
        pipeline_name: Display name of the pipeline.
        sort:          Display order in the Kommo UI.
        is_main:       True if this is the account's default pipeline.
        is_unsorted_on: Whether "unsorted" leads appear in this pipeline.
        is_archive:    Whether this pipeline is archived (read-only).
        account_id:    Kommo account ID.
        stages:        All stages belonging to this pipeline, sorted by `sort`.
        total_stages:  Computed count of stages.
        regular_stages: Stages with type=regular (excludes won/lost).
    """

    model_config = {"populate_by_name": True, "extra": "ignore"}

    # Core fields
    pipeline_id:   int        = Field(..., alias="id",          description="Pipeline ID")
    pipeline_name: str | None = Field(None, alias="name",       description="Pipeline display name")
    sort:          int | None = Field(None,                      description="UI display order")
    is_main:       bool       = Field(default=False,             description="Default pipeline?")
    is_unsorted_on: bool      = Field(default=False)
    is_archive:    bool       = Field(default=False,             description="Archived pipeline?")
    account_id:    int | None = Field(None)

    # Nested stages — populated by PipelinesExtractor, not from raw API dict
    stages: list[StageRecord] = Field(
        default_factory=list,
        description="Stages belonging to this pipeline, ordered by sort",
    )

    # Computed
    total_stages:   int = Field(default=0,  description="Total stage count")
    regular_stages: int = Field(default=0,  description="Non-terminal stage count (type=regular)")

    @model_validator(mode="after")
    def compute_stage_counts(self) -> "PipelineRecord":
        """Compute stage count helpers after stages are set."""
        self.total_stages   = len(self.stages)
        self.regular_stages = sum(1 for s in self.stages if s.stage_type == STAGE_TYPE_REGULAR)
        return self


# =============================================================================
# ExtractionResult
# =============================================================================

@dataclass
class PipelineExtractionResult:
    """
    Summary returned by PipelinesExtractor.extract_all().

    Attributes:
        total_pipelines:   Number of successfully extracted pipelines.
        total_stages:      Total stages across all pipelines.
        failed_pipelines:  Pipelines that failed Pydantic validation.
        output_path:       Path to the written JSON file.
        dead_letter_path:  Path to validation-failure file (None if no failures).
        duration_seconds:  Total extraction time.
    """

    total_pipelines:  int       = 0
    total_stages:     int       = 0
    failed_pipelines: int       = 0
    output_path:      Path | None = None
    dead_letter_path: Path | None = None
    duration_seconds: float     = 0.0
    started_at:       str       = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    finished_at:      str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_pipelines":  self.total_pipelines,
            "total_stages":     self.total_stages,
            "failed_pipelines": self.failed_pipelines,
            "output_path":      str(self.output_path),
            "dead_letter_path": str(self.dead_letter_path) if self.dead_letter_path else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "started_at":       self.started_at,
            "finished_at":      self.finished_at,
        }


# =============================================================================
# PipelinesExtractor
# =============================================================================

class PipelinesExtractor:
    """
    Extracts all pipelines and their stages from Kommo.

    No pagination required — accounts rarely have more than 20 pipelines.
    A single GET /api/v4/leads/pipelines returns all pipelines with
    their stages embedded.

    Args:
        client:     Open KommoAPIClient instance.
        output_dir: Directory for output JSON files.
    """

    def __init__(
        self,
        client: KommoAPIClient,
        output_dir: str | Path = _DEFAULT_OUTPUT_DIR,
    ) -> None:
        self._client     = client
        self._output_dir = Path(output_dir)
        self._error_dir  = self._output_dir / "errors"

    # =========================================================================
    # PUBLIC: Main Entry Point
    # =========================================================================

    @retry_api_call
    def extract_all(self) -> PipelineExtractionResult:
        """
        Fetch all pipelines (with embedded stages) and write to JSON.

        Returns:
            PipelineExtractionResult with counts, paths, and duration.

        Raises:
            KommoClientError: Unrecoverable API or network failure.
        """
        result  = PipelineExtractionResult()
        started = time.monotonic()

        logger.info("Pipeline extraction started")

        # ------------------------------------------------------------------
        # 1. Fetch all pipelines in a single API call
        # ------------------------------------------------------------------
        try:
            response = self._client.get(_KOMMO_API_PATH)
        except KommoNotFoundError:
            logger.info("No pipelines found (404) — account may have no leads pipelines")
            result.duration_seconds = time.monotonic() - started
            result.finished_at = datetime.now(tz=timezone.utc).isoformat()
            return result
        except KommoClientError as exc:
            logger.error(
                "Failed to fetch pipelines",
                extra={"status_code": exc.status_code, "error": str(exc)},
            )
            raise

        if response.is_empty:
            logger.info("Pipelines endpoint returned 204 — no pipelines found")
            result.duration_seconds = time.monotonic() - started
            result.finished_at = datetime.now(tz=timezone.utc).isoformat()
            return result

        raw_pipelines = response.embedded(_EMBEDDED_PIPELINES)
        logger.info("Raw pipelines fetched", extra={"count": len(raw_pipelines)})

        # ------------------------------------------------------------------
        # 2. Validate and build PipelineRecord objects
        # ------------------------------------------------------------------
        valid_pipelines: list[dict[str, Any]] = []
        failed_pipelines: list[dict[str, Any]] = []

        for raw_pipeline in raw_pipelines:
            pipeline_record, failures = self._parse_pipeline(raw_pipeline)
            if pipeline_record is not None:
                valid_pipelines.append(pipeline_record.model_dump(mode="json", by_alias=False))
                result.total_stages += pipeline_record.total_stages
                logger.debug(
                    "Pipeline parsed",
                    extra={
                        "pipeline_id":   pipeline_record.pipeline_id,
                        "pipeline_name": pipeline_record.pipeline_name,
                        "stages":        pipeline_record.total_stages,
                    },
                )
            else:
                failed_pipelines.extend(failures)

        result.total_pipelines  = len(valid_pipelines)
        result.failed_pipelines = len(failed_pipelines)

        # ------------------------------------------------------------------
        # 3. Write valid pipelines to JSON
        # ------------------------------------------------------------------
        if valid_pipelines:
            result.output_path = self._write_json(
                filename="pipelines.json",
                records=valid_pipelines,
                total_stages=result.total_stages,
            )

        # ------------------------------------------------------------------
        # 4. Write dead-letter (validation failures)
        # ------------------------------------------------------------------
        if failed_pipelines:
            result.dead_letter_path = self._write_dead_letter(failed_pipelines)
            logger.warning(
                "Some pipelines failed validation",
                extra={
                    "failed_count":    len(failed_pipelines),
                    "dead_letter_path": str(result.dead_letter_path),
                },
            )

        # ------------------------------------------------------------------
        # 5. Finalise
        # ------------------------------------------------------------------
        result.duration_seconds = time.monotonic() - started
        result.finished_at = datetime.now(tz=timezone.utc).isoformat()

        logger.info("Pipeline extraction complete", extra=result.as_dict())
        return result

    # =========================================================================
    # PUBLIC: Lookup Helpers
    # =========================================================================

    def build_lookup(
        self,
        pipelines: list[dict[str, Any]],
    ) -> dict[int, dict[str, Any]]:
        """
        Build a pipeline_id → pipeline dict lookup map.

        Useful for enriching lead records without repeated iteration.

        Args:
            pipelines: List of serialised PipelineRecord dicts
                       (as returned in outputs/pipelines.json data array).

        Returns:
            Dict: { pipeline_id: { pipeline_name, stages, ... } }

        Example:
            lookup = extractor.build_lookup(result_data)
            pipeline_name = lookup[lead["pipeline_id"]]["pipeline_name"]
        """
        return {p["pipeline_id"]: p for p in pipelines}

    def build_stage_lookup(
        self,
        pipelines: list[dict[str, Any]],
    ) -> dict[int, dict[str, Any]]:
        """
        Build a flat stage_id → stage dict lookup across all pipelines.

        Args:
            pipelines: List of serialised PipelineRecord dicts.

        Returns:
            Dict: { stage_id: { stage_name, stage_type, pipeline_id, ... } }

        Example:
            stage_lookup = extractor.build_stage_lookup(result_data)
            stage_name = stage_lookup[lead["status_id"]]["stage_name"]
        """
        lookup: dict[int, dict[str, Any]] = {}
        for pipeline in pipelines:
            for stage in pipeline.get("stages", []):
                lookup[stage["stage_id"]] = stage
        return lookup

    # =========================================================================
    # PRIVATE: Parsing
    # =========================================================================

    def _parse_pipeline(
        self,
        raw: dict[str, Any],
    ) -> tuple[PipelineRecord | None, list[dict[str, Any]]]:
        """
        Parse a single raw pipeline dict into a PipelineRecord with stages.

        Handles Kommo's nested structure:
            raw["_embedded"]["statuses"] → list of stage dicts

        Args:
            raw: Raw pipeline dict from _embedded.pipelines API response.

        Returns:
            (PipelineRecord, []) on success.
            (None, [failed_entry]) on validation failure.
        """
        from pydantic import ValidationError

        pipeline_id = raw.get("id", "unknown")

        # ------------------------------------------------------------------
        # Extract and validate stages from _embedded.statuses
        # ------------------------------------------------------------------
        raw_stages: list[dict[str, Any]] = (
            raw.get("_embedded", {}).get(_EMBEDDED_STATUSES, [])
        )

        valid_stages: list[StageRecord] = []
        stage_failures: list[dict[str, Any]] = []

        for raw_stage in raw_stages:
            # Inject pipeline_id into stage (Kommo includes it, but be defensive)
            raw_stage.setdefault("pipeline_id", pipeline_id)
            try:
                stage = StageRecord.model_validate(raw_stage)
                valid_stages.append(stage)
            except ValidationError as exc:
                stage_id = raw_stage.get("id", "unknown")
                logger.warning(
                    "Stage validation failed",
                    extra={
                        "pipeline_id": pipeline_id,
                        "stage_id":    stage_id,
                        "errors":      exc.errors(include_url=False),
                    },
                )
                stage_failures.append({
                    "_raw":               raw_stage,
                    "_validation_errors": exc.errors(include_url=False),
                    "_parent_pipeline_id": pipeline_id,
                })

        # Sort stages by their sort order for predictable output
        valid_stages.sort(key=lambda s: s.sort or 0)

        # ------------------------------------------------------------------
        # Validate the pipeline itself
        # ------------------------------------------------------------------
        try:
            pipeline = PipelineRecord.model_validate(raw)
            pipeline.stages = valid_stages
            pipeline.total_stages   = len(valid_stages)
            pipeline.regular_stages = sum(
                1 for s in valid_stages if s.stage_type == STAGE_TYPE_REGULAR
            )
            return pipeline, []

        except ValidationError as exc:
            logger.warning(
                "Pipeline validation failed",
                extra={
                    "pipeline_id": pipeline_id,
                    "errors":      exc.errors(include_url=False),
                },
            )
            return None, [{
                "_raw":               raw,
                "_validation_errors": exc.errors(include_url=False),
            }]

    # =========================================================================
    # PRIVATE: File I/O
    # =========================================================================

    def _write_json(
        self,
        filename: str,
        records: list[dict[str, Any]],
        total_stages: int = 0,
    ) -> Path:
        """
        Atomically write pipeline records to a JSON file.

        Output envelope:
            {
                "_meta": { count, total_stages, extracted_at, ... },
                "data": [ ... ]
            }
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / filename

        envelope: dict[str, Any] = {
            "_meta": {
                "entity":        "pipelines",
                "count":         len(records),
                "total_stages":  total_stages,
                "extracted_at":  datetime.now(tz=timezone.utc).isoformat(),
                "source":        "kommo_api_v4",
            },
            "data": records,
        }

        self._atomic_write(output_path, envelope)
        logger.info(
            "Pipelines written to disk",
            extra={"path": str(output_path), "count": len(records)},
        )
        return output_path

    def _write_dead_letter(self, failed: list[dict[str, Any]]) -> Path:
        """Write validation-failed pipeline records to a dead-letter file."""
        self._error_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        dl_path = self._error_dir / f"pipelines_failed_{ts}.json"

        envelope: dict[str, Any] = {
            "_meta": {
                "entity":     "pipelines",
                "type":       "dead_letter",
                "count":      len(failed),
                "created_at": datetime.now(tz=timezone.utc).isoformat(),
                "note":       "These pipeline records failed Pydantic validation.",
            },
            "data": failed,
        }

        self._atomic_write(dl_path, envelope)
        return dl_path

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        """Write JSON to a temp file then atomically rename to the target."""
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
