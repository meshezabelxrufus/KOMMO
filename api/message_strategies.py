"""
api/message_strategies.py
==========================
Multi-strategy fallback message extractor for Kommo accounts where
/api/v4/chats returns 404 (Kommo Conversations feature not enabled).

DIAGNOSTIC CONTEXT  (clinicabelba account — confirmed 2026-05-26)
──────────────────
  /api/v4/chats                    → 404  (feature not enabled)
  /api/v4/chats/{id}/messages      → 404  (feature not enabled)
  /api/v4/talks/{id}/messages      → 403  (insufficient scope)
  /api/v4/talks                    → ✅ 250+ records per page
  /api/v4/events?type=*_chat_msg   → ✅ 500+ message events
  /api/v4/leads/{id}/notes         → ✅ Agent notes with real text

STRATEGY CHAIN
──────────────
  1. Events API  — Paginate outgoing + incoming_chat_message events.
                   Each event carries: lead_id, direction, timestamp,
                   origin (waba / com.wazzup.whatsapp), message_id.
                   Message TEXT is not accessible (stored in Wazzup/WABA).
                   Produces: one flat record per event.

  2. Notes API   — For every lead that appeared in events, fetch all notes.
                   note_type=common (4) → agent observations / text.
                   Produces: supplementary flat records WITH real text.

  3. Talks API   — Fetch all talks for channel enrichment.
                   Maps lead_id → channel label (WhatsApp WABA, Wazzup, …).
                   Used to enrich event records with accurate channel name.

OUTPUT SCHEMA  (identical to ChatsExtractor flat schema)
─────────────
  lead_id, lead_name, contact_name, channel, direction,
  author, message_text, timestamp, timestamp_iso,
  message_id, chat_id, channel_raw, author_type, author_id,
  media_url, extraction_source

USAGE
─────
    from api.message_strategies import FallbackMessageExtractor

    fb = FallbackMessageExtractor(
        client=client,
        lead_names={25880530: "Maria Lopez", ...},
        contact_names={25880530: "Maria Lopez", ...},
        output_dir="outputs",
    )
    result = fb.extract()
    # result.flat_messages  — list of flat dicts
    # result.virtual_chats  — list of virtual chat-level dicts
    # result.stats          — dict with counts
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Generator

from api.client import KommoAPIClient, KommoClientError, KommoNotFoundError
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Channel label mapping
# ---------------------------------------------------------------------------

_ORIGIN_LABELS: dict[str, str] = {
    "com.wazzup.whatsapp": "WhatsApp (Wazzup)",
    "waba":                "WhatsApp Business API",
    "waba_v2":             "WhatsApp Business API",
    "whatsapp":            "WhatsApp",
    "instagram":           "Instagram",
    "telegram":            "Telegram",
    "email":               "Email",
    "sms":                 "SMS",
    "note":                "Internal Note",
}

# Events API filter values
_CHAT_EVENT_TYPES = ["outgoing_chat_message", "incoming_chat_message"]

# Note types that carry text content
_TEXT_NOTE_TYPES = {"common", "4", 4}

# Maximum pages to paginate (safety cap)
_MAX_PAGES = 500


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class FallbackExtractionResult:
    """Result of a fallback multi-strategy message extraction."""
    flat_messages:     list[dict[str, Any]] = field(default_factory=list)
    virtual_chats:     list[dict[str, Any]] = field(default_factory=list)
    stats:             dict[str, int]        = field(default_factory=dict)
    strategies_used:   list[str]             = field(default_factory=list)
    duration_seconds:  float                 = 0.0
    warnings:          list[str]             = field(default_factory=list)


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------

class FallbackMessageExtractor:
    """
    Multi-strategy message extractor for Kommo accounts without Chats API.

    Chains three available data sources into a single unified flat schema
    that is drop-in compatible with ChatsExtractor output.

    Args:
        client:        Open KommoAPIClient instance.
        lead_names:    {lead_id: lead_name} enrichment map.
        contact_names: {lead_id: contact_name} enrichment map.
        page_size:     Records per API page (max 250).
    """

    def __init__(
        self,
        client:        KommoAPIClient,
        lead_names:    dict[int, str] | None = None,
        contact_names: dict[int, str] | None = None,
        page_size:     int = 250,
        since_days:    int = 1,
    ) -> None:
        self._client        = client
        self._lead_names    = lead_names or {}
        self._contact_names = contact_names or {}
        self._page_size     = min(page_size, 250)
        # Unix timestamp for the start of the events window
        self._since_ts: int = int(
            (datetime.now(tz=timezone.utc) - timedelta(days=since_days)).timestamp()
        )
        logger.info(
            "[Fallback] Events window: last %d days (from %s)",
            since_days,
            datetime.fromtimestamp(self._since_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        )

    # =========================================================================
    # PUBLIC
    # =========================================================================

    def extract(self) -> FallbackExtractionResult:
        """
        Run the full fallback extraction chain.

        Returns:
            FallbackExtractionResult with flat_messages, virtual_chats, stats.
        """
        started = time.monotonic()
        result  = FallbackExtractionResult()

        # ── Step 1: Talks — build channel map ─────────────────────────────
        logger.info("[Fallback] Step 1 — fetching talks for channel enrichment")
        talks_map, total_talks = self._fetch_talks_map()
        result.strategies_used.append("talks_metadata")
        logger.info("[Fallback] Talks fetched", extra={"total": total_talks})

        # ── Step 2: Events — primary message records ──────────────────────
        logger.info("[Fallback] Step 2 — fetching chat message events")
        event_records, event_lead_ids = self._fetch_event_messages(talks_map)
        result.strategies_used.append("events_api")
        logger.info(
            "[Fallback] Events fetched",
            extra={"records": len(event_records), "unique_leads": len(event_lead_ids)},
        )

        # ── Step 3: Notes — supplementary text per active lead ────────────
        logger.info(
            "[Fallback] Step 3 — fetching notes for %d leads", len(event_lead_ids)
        )
        note_records = self._fetch_lead_notes(event_lead_ids, talks_map)
        result.strategies_used.append("notes_api")
        logger.info("[Fallback] Notes fetched", extra={"records": len(note_records)})

        # ── Step 4: Merge + deduplicate ───────────────────────────────────
        all_flat = event_records + note_records
        all_flat = self._deduplicate(all_flat)
        all_flat.sort(key=lambda m: m.get("timestamp") or 0)

        # ── Step 5: Build virtual chats (one per lead) ────────────────────
        virtual_chats = self._build_virtual_chats(all_flat, talks_map)

        # ── Populate result ───────────────────────────────────────────────
        result.flat_messages   = all_flat
        result.virtual_chats   = virtual_chats
        result.duration_seconds = time.monotonic() - started

        inbound  = sum(1 for m in all_flat if m.get("direction") == "inbound")
        outbound = sum(1 for m in all_flat if m.get("direction") == "outbound")

        result.stats = {
            "total_messages":    len(all_flat),
            "inbound_messages":  inbound,
            "outbound_messages": outbound,
            "total_talks":       total_talks,
            "event_records":     len(event_records),
            "note_records":      len(note_records),
            "unique_leads":      len(event_lead_ids),
            "virtual_chats":     len(virtual_chats),
        }

        logger.info(
            "[Fallback] Extraction complete",
            extra={
                "total_messages": len(all_flat),
                "inbound":        inbound,
                "outbound":       outbound,
                "duration_s":     round(result.duration_seconds, 2),
            },
        )
        return result

    # =========================================================================
    # STRATEGY 1 — Talks (channel enrichment map)
    # =========================================================================

    def _fetch_talks_map(self) -> tuple[dict[int, dict[str, Any]], int]:
        """
        Fetch all talks and build a lead_id → talk metadata map.

        Returns:
            (talks_map, total_talks)
            talks_map: {lead_id: {talk_id, origin, channel, contact_id, status}}
        """
        talks_map:  dict[int, dict[str, Any]] = {}
        total_talks = 0

        try:
            for page in self._client.paginate(
                path="/talks",
                resource="talks",
                page_size=self._page_size,
                max_pages=_MAX_PAGES,
            ):
                for talk in page:
                    lid = talk.get("entity_id")
                    if lid is None:
                        continue
                    lid = int(lid)
                    origin  = talk.get("origin") or ""
                    channel = _ORIGIN_LABELS.get(origin, origin or "Unknown")

                    # Only update if we don't have an entry or this one is newer
                    existing = talks_map.get(lid)
                    talk_ts  = talk.get("updated_at") or talk.get("created_at") or 0
                    if not existing or talk_ts > existing.get("_ts", 0):
                        talks_map[lid] = {
                            "talk_id":    talk.get("talk_id"),
                            "chat_id":    talk.get("chat_id"),
                            "origin":     origin,
                            "channel":    channel,
                            "contact_id": talk.get("contact_id"),
                            "status":     talk.get("status"),
                            "source_id":  talk.get("source_id"),
                            "_ts":        talk_ts,
                        }
                    total_talks += 1

        except KommoClientError as exc:
            logger.warning("[Fallback] Talks fetch error: %s", exc)

        return talks_map, total_talks

    # =========================================================================
    # STRATEGY 2 — Events API (actual WhatsApp activity records)
    # =========================================================================

    def _fetch_event_messages(
        self,
        talks_map: dict[int, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[int]]:
        """
        Fetch all outgoing/incoming chat message events and build flat records.

        Returns:
            (flat_records, lead_ids_seen)
        """
        flat: list[dict[str, Any]] = []
        lead_ids_seen: set[int]    = set()

        for event_type in _CHAT_EVENT_TYPES:
            direction = "outbound" if "outgoing" in event_type else "inbound"
            logger.debug("[Fallback] Paginating events type=%s", event_type)

            try:
                for page in self._client.paginate(
                    path="/events",
                    resource="events",
                    page_size=self._page_size,
                    max_pages=_MAX_PAGES,
                    params={
                        "filter[type][]": event_type,
                        "filter[created_at][from]": self._since_ts,
                    },
                ):
                    for evt in page:
                        record = self._event_to_flat(evt, direction, talks_map)
                        if record:
                            flat.append(record)
                            if record.get("lead_id"):
                                lead_ids_seen.add(int(record["lead_id"]))

            except KommoClientError as exc:
                logger.warning(
                    "[Fallback] Events fetch error for type=%s: %s",
                    event_type, exc,
                )

        return flat, lead_ids_seen

    def _event_to_flat(
        self,
        evt:       dict[str, Any],
        direction: str,
        talks_map: dict[int, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Convert a single event dict to a flat message record."""
        lead_id   = evt.get("entity_id")
        if lead_id is None:
            return None
        lead_id = int(lead_id)

        ts = evt.get("created_at")
        ts_iso = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if ts else None
        )

        # Extract message metadata from value_after
        value_after = evt.get("value_after") or []
        msg_meta    = {}
        if isinstance(value_after, list) and value_after:
            first = value_after[0]
            if isinstance(first, dict):
                msg_meta = first.get("message", {}) or {}
        elif isinstance(value_after, dict):
            msg_meta = value_after.get("message", {}) or {}

        msg_id   = msg_meta.get("id")
        origin   = msg_meta.get("origin") or ""
        talk_id  = msg_meta.get("talk_id")

        # Resolve channel from talks_map or event origin
        talk_info = talks_map.get(lead_id, {})
        channel_raw = origin or talk_info.get("origin") or ""
        channel     = _ORIGIN_LABELS.get(channel_raw, channel_raw or "WhatsApp")

        # Message text — not accessible via this API path; use descriptive marker
        origin_label = _ORIGIN_LABELS.get(origin, origin or "WhatsApp")
        message_text = f"[{direction.title()} {origin_label} message]"

        return {
            # Standard flat schema fields
            "lead_id":       lead_id,
            "lead_name":     self._lead_names.get(lead_id),
            "contact_name":  self._contact_names.get(lead_id),
            "channel":       channel,
            "direction":     direction,
            "author":        None,
            "message_text":  message_text,
            "timestamp":     ts,
            "timestamp_iso": ts_iso,
            # Extended fields
            "message_id":     msg_id,
            "talk_id":        talk_id or talk_info.get("talk_id"),
            "chat_id":        talk_info.get("chat_id"),
            "channel_raw":    channel_raw,
            "author_type":    None,
            "author_id":      None,
            "media_url":      None,
            # Provenance
            "extraction_source": "events_api",
        }

    # =========================================================================
    # STRATEGY 3 — Lead Notes (supplementary real text)
    # =========================================================================

    def _fetch_lead_notes(
        self,
        lead_ids:  set[int],
        talks_map: dict[int, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Fetch all notes for every lead that had message event activity.

        Notes (type=common) represent agent observations and text entered
        during or after WhatsApp conversations. They provide real text
        content when the Chats API is unavailable.

        Args:
            lead_ids:  Set of lead IDs to fetch notes for.
            talks_map: Talks metadata for channel enrichment.

        Returns:
            List of flat message records from notes.
        """
        flat: list[dict[str, Any]] = []

        for lead_id in sorted(lead_ids):
            try:
                notes = self._fetch_notes_for_lead(lead_id)
                for note in notes:
                    record = self._note_to_flat(note, lead_id, talks_map)
                    if record:
                        flat.append(record)
            except KommoClientError as exc:
                logger.debug(
                    "[Fallback] Notes error for lead %d: %s", lead_id, exc
                )

        return flat

    def _fetch_notes_for_lead(self, lead_id: int) -> list[dict[str, Any]]:
        """Fetch all notes for a single lead via pagination."""
        notes: list[dict[str, Any]] = []
        try:
            for page in self._client.paginate(
                path=f"/leads/{lead_id}/notes",
                resource="notes",
                page_size=self._page_size,
            ):
                notes.extend(page)
        except KommoNotFoundError:
            pass  # Lead has no notes
        return notes

    def _note_to_flat(
        self,
        note:      dict[str, Any],
        lead_id:   int,
        talks_map: dict[int, dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Convert a lead note to a flat message record."""
        note_type = note.get("note_type")

        # Only include text-bearing notes
        params = note.get("params") or {}
        text   = None

        if isinstance(params, dict):
            text = (
                params.get("text")
                or params.get("body")
                or params.get("note")
            )
        elif isinstance(params, str):
            text = params

        if not text:
            return None  # Skip empty or non-text notes

        ts = note.get("created_at")
        ts_iso = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if ts else None
        )

        # Determine direction from note type (Kommo note_type codes):
        # 102 = incoming message   103 = outgoing message   4/common = agent note
        nt_str = str(note_type)
        if nt_str == "102":
            direction = "inbound"
        elif nt_str in ("103", "common", "4"):
            direction = "outbound"
        else:
            direction = "outbound"  # Default: agent wrote it

        # Channel from talks_map or note origin
        talk_info   = talks_map.get(lead_id, {})
        channel_raw = talk_info.get("origin") or "note"
        channel     = (
            talk_info.get("channel")
            or _ORIGIN_LABELS.get(channel_raw, "Internal Note")
        )
        if note_type == 4 or nt_str == "common":
            channel = "Internal Note"

        # Author
        created_by = note.get("created_by")

        return {
            "lead_id":       lead_id,
            "lead_name":     self._lead_names.get(lead_id),
            "contact_name":  self._contact_names.get(lead_id),
            "channel":       channel,
            "direction":     direction,
            "author":        str(created_by) if created_by else None,
            "message_text":  text.strip(),
            "timestamp":     ts,
            "timestamp_iso": ts_iso,
            "message_id":    str(note.get("id") or ""),
            "talk_id":       talk_info.get("talk_id"),
            "chat_id":       talk_info.get("chat_id"),
            "channel_raw":   channel_raw,
            "author_type":   "user",
            "author_id":     created_by,
            "media_url":     None,
            "extraction_source": "notes_api",
        }

    # =========================================================================
    # STEP 4 — Deduplication
    # =========================================================================

    def _deduplicate(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Remove duplicate records.

        Deduplication key: (lead_id, timestamp, direction, message_id)
        Events and notes should not overlap, but be defensive.
        """
        seen: set[tuple] = set()
        out:  list[dict[str, Any]] = []

        for r in records:
            key = (
                r.get("lead_id"),
                r.get("timestamp"),
                r.get("direction"),
                r.get("message_id") or r.get("message_text", "")[:40],
            )
            if key not in seen:
                seen.add(key)
                out.append(r)

        removed = len(records) - len(out)
        if removed:
            logger.debug("[Fallback] Deduplication removed %d duplicates", removed)
        return out

    # =========================================================================
    # STEP 5 — Virtual Chat Builder
    # =========================================================================

    def _build_virtual_chats(
        self,
        flat:      list[dict[str, Any]],
        talks_map: dict[int, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Build one virtual chat record per lead from flat messages.

        This produces the chats.json output — a chat-level aggregate
        compatible with the original ChatsExtractor chats.json format.

        Args:
            flat:      All flat message records.
            talks_map: Talks metadata map.

        Returns:
            List of virtual chat dicts, one per unique lead.
        """
        from collections import defaultdict

        by_lead: dict[int, list[dict]] = defaultdict(list)
        for msg in flat:
            lid = msg.get("lead_id")
            if lid is not None:
                by_lead[int(lid)].append(msg)

        chats: list[dict[str, Any]] = []
        for lead_id, msgs in by_lead.items():
            msgs_sorted = sorted(msgs, key=lambda m: m.get("timestamp") or 0)
            last_ts     = msgs_sorted[-1].get("timestamp") if msgs_sorted else None
            talk_info   = talks_map.get(lead_id, {})

            chats.append({
                "id":             str(talk_info.get("chat_id") or f"virtual-{lead_id}"),
                "entity_id":      lead_id,
                "entity_type":    "lead",
                "lead_name":      self._lead_names.get(lead_id),
                "contact_name":   self._contact_names.get(lead_id),
                "channel_type":   talk_info.get("origin") or "unknown",
                "channel":        talk_info.get("channel") or "Unknown",
                "talk_id":        talk_info.get("talk_id"),
                "last_message_at": last_ts,
                "total_messages": len(msgs),
                "inbound":  sum(1 for m in msgs if m.get("direction") == "inbound"),
                "outbound": sum(1 for m in msgs if m.get("direction") == "outbound"),
                "has_text_notes": any(
                    m.get("extraction_source") == "notes_api" for m in msgs
                ),
                "extraction_source": "fallback_chain",
            })

        # Sort by last activity descending
        chats.sort(key=lambda c: c.get("last_message_at") or 0, reverse=True)
        return chats
