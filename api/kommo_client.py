"""
api/kommo_client.py
===================
Kommo-specific API client with typed endpoint methods.

Wraps BaseClient to provide high-level, domain-aware methods:
  - get_leads()     → Paginated lead records
  - get_pipelines() → All pipelines with embedded stages
  - get_tasks()     → Paginated task records

Kommo API conventions handled here:
  - All list responses are nested under response._embedded.{resource}
  - Pagination via `page` + `limit` query params (max 250)
  - HTTP 204 No Content means the page is empty (end of data)
  - `with[]` param used to fetch related data in one request (avoid N+1)
  - Custom field definitions fetched once and cached

Usage:
    from api.kommo_client import KommoClient
    from api.base_client import BaseClient

    with BaseClient(settings, token_mgr, limiter) as http:
        client = KommoClient(http)
        leads_page = client.get_leads(page=1)
        pipelines = client.get_pipelines()
"""

from __future__ import annotations

from typing import Any

from api.base_client import BaseClient
from utils.logger import get_logger

log = get_logger(__name__)

# Kommo API resource names (used in _embedded response key)
_LEADS_RESOURCE = "leads"
_PIPELINES_RESOURCE = "pipelines"
_TASKS_RESOURCE = "tasks"


class KommoClient:
    """
    Kommo CRM API client — domain-specific methods over BaseClient.

    Args:
        http: An open BaseClient instance (used as a context manager).
    """

    def __init__(self, http: BaseClient) -> None:
        self._http = http

    # ------------------------------------------------------------------
    # Leads
    # ------------------------------------------------------------------

    def get_leads(
        self,
        page: int = 1,
        limit: int = 250,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch one page of leads from /api/v4/leads.

        Args:
            page:    Page number (1-indexed).
            limit:   Records per page (max 250).
            filters: Optional Kommo filter params (e.g. {"filter[updated_at][from]": ts})

        Returns:
            List of raw lead dicts from _embedded.leads.
            Empty list if the page has no records (HTTP 204 or empty _embedded).

        Raises:
            KommoAPIError: On non-2xx / non-204 responses.
        """
        # TODO: Implement GET /leads with page, limit, filters
        # Include with[]=contacts&with[]=tags for enrichment
        raise NotImplementedError("get_leads — to be implemented in Phase 3")

    # ------------------------------------------------------------------
    # Pipelines & Stages
    # ------------------------------------------------------------------

    def get_pipelines(self) -> list[dict[str, Any]]:
        """
        Fetch all pipelines (with embedded stages) from /api/v4/leads/pipelines.

        Pipelines are typically < 50 records — no pagination needed.
        Stages are embedded in each pipeline object under _embedded.statuses.

        Returns:
            List of raw pipeline dicts (each containing embedded stages).
        """
        # TODO: Implement GET /leads/pipelines
        raise NotImplementedError("get_pipelines — to be implemented in Phase 3")

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def get_tasks(
        self,
        page: int = 1,
        limit: int = 250,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch one page of tasks from /api/v4/tasks.

        Args:
            page:    Page number (1-indexed).
            limit:   Records per page (max 250).
            filters: Optional Kommo filter params.

        Returns:
            List of raw task dicts from _embedded.tasks.
            Empty list if the page has no records.
        """
        # TODO: Implement GET /tasks with page, limit, filters
        raise NotImplementedError("get_tasks — to be implemented in Phase 3")

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _extract_embedded(
        self,
        response_json: dict[str, Any],
        resource: str,
    ) -> list[dict[str, Any]]:
        """
        Safely extract the resource list from a Kommo _embedded response.

        Kommo wraps all list resources as:
            { "_embedded": { "<resource>": [...] } }

        Args:
            response_json: Parsed JSON from API response.
            resource:      Key inside _embedded (e.g. "leads", "tasks").

        Returns:
            List of resource dicts, or empty list if not present.
        """
        # TODO: Implement safe _embedded extraction
        raise NotImplementedError
