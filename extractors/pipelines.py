"""
extractors/pipelines.py
=======================
Pipeline and stage extraction from Kommo.

Pipelines don't require pagination (accounts rarely have more than 10–20
pipelines). Stages are embedded inside each pipeline response under
`_embedded.statuses` — no separate endpoint is needed.

The extracted PipelineModel includes a nested list of StageModel objects,
giving a complete hierarchical snapshot of the account's sales process.

Usage:
    from extractors.pipelines import PipelineExtractor

    extractor = PipelineExtractor(client=kommo_client, writer=json_writer)
    pipelines = extractor.extract_all()
    # pipelines is List[PipelineModel] with stages nested inside
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from api.kommo_client import KommoClient
from outputs.json_writer import JsonWriter
from utils.logger import get_logger

log = get_logger(__name__)


# =============================================================================
# Pydantic Models
# =============================================================================

class StageModel(BaseModel):
    """
    Validated representation of a Kommo pipeline stage (status).

    In Kommo's API, pipeline stages are called "statuses".
    """

    id: int = Field(..., description="Stage ID")
    name: str | None = Field(None, description="Stage display name")
    sort: int | None = Field(None, description="Sort order in pipeline")
    is_editable: bool = Field(default=True)
    pipeline_id: int | None = Field(None, description="Parent pipeline ID")
    color: str | None = Field(None, description="Stage colour hex code (e.g. #ffffa8)")
    type: int | None = Field(None, description="Stage type: 0=regular, 1=won, 2=lost")
    account_id: int | None = Field(None)


class PipelineModel(BaseModel):
    """
    Validated representation of a Kommo pipeline with embedded stages.
    """

    id: int = Field(..., description="Pipeline ID")
    name: str | None = Field(None, description="Pipeline display name")
    sort: int | None = Field(None, description="Sort order")
    is_main: bool = Field(default=False, description="Is this the default pipeline?")
    is_unsorted_on: bool = Field(default=False)
    is_archive: bool = Field(default=False)
    account_id: int | None = Field(None)
    stages: list[StageModel] = Field(
        default_factory=list,
        description="Stages (statuses) belonging to this pipeline",
    )


# =============================================================================
# Extractor
# =============================================================================

class PipelineExtractor:
    """
    Extracts all pipelines and their stages from Kommo.

    No pagination required — pipelines are a small dataset.

    Args:
        client: KommoClient instance (open HTTP session).
        writer: JsonWriter for persisting results.
    """

    def __init__(self, client: KommoClient, writer: JsonWriter) -> None:
        self._client = client
        self._writer = writer

    def extract_all(self) -> list[PipelineModel]:
        """
        Fetch all pipelines with embedded stages.

        Returns:
            List of PipelineModel instances (each with nested stages).

        Raises:
            KommoAPIError: On unrecoverable API errors.
        """
        # TODO: Implement
        # raw_pipelines = self._client.get_pipelines()
        # validated, failed = self._validate_records(raw_pipelines)
        # if failed: self._writer.write_dead_letter("pipelines", failed)
        # return validated
        raise NotImplementedError("PipelineExtractor.extract_all — to be implemented in Phase 4")

    def _validate_records(
        self,
        raw_records: list[dict[str, Any]],
    ) -> tuple[list[PipelineModel], list[dict[str, Any]]]:
        """Validate raw pipeline dicts, extracting nested stages."""
        # TODO: Implement per-record Pydantic validation
        # Note: stages come from raw["_embedded"]["statuses"] — map to StageModel
        raise NotImplementedError
