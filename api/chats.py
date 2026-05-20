"""
api/chats.py
============
Chat and message extraction for the Kommo CRM integration.

WHAT THIS EXTRACTS
──────────────────
Kommo's Chats API provides full conversation history across all
connected channels: WhatsApp, Instagram, Telegram, email, SMS,
and internal notes.

This module extracts:
  1. ChatRecord      — one conversation thread (linked to a lead)
  2. MessageRecord   — one message within a conversation
  3. FlatMessage     — denormalised record joining chat + message fields
                       (the format consumed by the AI analysis pipeline)

The FlatMessage schema matches the Milestone 1 specification:
    lead_id, lead_name, contact_name, channel, direction,
    author, message_text, timestamp

KOMMO API NOTES
───────────────
  Conversations: GET /api/v4/chats
    Response: { "_embedded": { "chats": [...] } }
    Each chat has: id, entity_type, entity_id (lead ID), last_message

  Messages: GET /api/v4/chats/{chat_id}/messages
    Response: { "_embedded": { "messages": [...] } }
    Each message has: id, chat_id, created_at, author, body, direction

  IMPORTANT: Kommo does not expose a single paginated messages endpoint.
  Messages must be fetched per-chat. For accounts with thousands of chats,
  incremental sync (fetching only chats updated since last run) is critical.

INCREMENTAL SYNC
────────────────
  Use last_message_timestamp from StateManager as the filter:
    GET /api/v4/chats?filter[last_message_at][from]=<timestamp>

  This returns only chats that had activity since the last sync,
  preventing re-fetching the entire conversation history.

USAGE
─────
    from auth.oauth import KommoOAuthClient
    from api.client import KommoAPIClient
    from api.chats import ChatsExtractor

    oauth = KommoOAuthClient()
    with KommoAPIClient(oauth) as client:
        extractor = ChatsExtractor(client, output_dir="outputs")
        result    = extractor.extract_all()

    print(f"Extracted {result.total_messages} messages from {result.total_chats} chats")
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from api.client import KommoAPIClient, KommoClientError, KommoNotFoundError
from utils.logger import get_logger
from utils.retry import retry_api_call

logger = get_logger(__name__)

_DEFAULT_PAGE_SIZE  = 250
_DEFAULT_OUTPUT_DIR = Path("outputs")

# Kommo channel type labels
_CHANNEL_LABELS: dict[str, str] = {
    "whatsapp":  "WhatsApp",
    "instagram": "Instagram",
    "telegram":  "Telegram",
    "email":     "Email",
    "sms":       "SMS",
    "note":      "Internal Note",
    "call":      "Call",
}


# =============================================================================
# Pydantic Models
# =============================================================================

class MessageAuthor(BaseModel):
    """Author of a chat message."""
    model_config = {"extra": "ignore"}
    id:          int | None  = None
    type:        str | None  = None  # "user" | "contact" | "bot"
    name:        str | None  = None


class MessageRecord(BaseModel):
    """A single message within a Kommo chat conversation."""
    model_config = {"extra": "ignore"}

    id:          str        = Field(..., description="Message UUID")
    chat_id:     str        = Field(..., description="Parent chat UUID")
    created_at:  int | None = Field(None, description="Message creation Unix timestamp")
    author:      MessageAuthor | None = None
    body:        str | None = Field(None, description="Message text content")
    media_url:   str | None = Field(None, description="Attached media URL (if any)")
    direction:   str | None = Field(None, description="'in' (inbound) or 'out' (outbound)")
    origin:      str | None = Field(None, description="Channel origin (whatsapp, email, etc.)")
    is_read:     bool       = Field(default=False)

    created_at_iso: str | None = None

    @model_validator(mode="after")
    def compute_iso(self) -> "MessageRecord":
        if self.created_at:
            self.created_at_iso = datetime.fromtimestamp(
                self.created_at, tz=timezone.utc
            ).isoformat()
        return self


class ChatRecord(BaseModel):
    """A Kommo chat conversation thread."""
    model_config = {"extra": "ignore"}

    id:                str        = Field(..., description="Chat UUID")
    entity_id:         int | None = Field(None, description="Linked lead/contact ID")
    entity_type:       str | None = Field(None, description="'lead' or 'contact'")
    channel_type:      str | None = Field(None, description="Channel: whatsapp, email, etc.")
    created_at:        int | None = None
    updated_at:        int | None = None
    last_message_at:   int | None = Field(None, description="Timestamp of last message")

    created_at_iso:      str | None = None
    last_message_at_iso: str | None = None

    @model_validator(mode="after")
    def compute_iso(self) -> "ChatRecord":
        if self.created_at:
            self.created_at_iso = datetime.fromtimestamp(
                self.created_at, tz=timezone.utc
            ).isoformat()
        if self.last_message_at:
            self.last_message_at_iso = datetime.fromtimestamp(
                self.last_message_at, tz=timezone.utc
            ).isoformat()
        return self


class FlatMessage(BaseModel):
    """
    Denormalised message record — the AI-ready output schema.

    Joins ChatRecord + MessageRecord + caller-supplied lead/contact metadata
    into a single flat record that Claude can consume directly.

    Matches the Milestone 1 specification:
        lead_id, lead_name, contact_name, channel,
        direction, author, message_text, timestamp
    """
    model_config = {"extra": "ignore"}

    # Identity
    message_id:    str
    chat_id:       str
    lead_id:       int | None = None
    lead_name:     str | None = None
    contact_name:  str | None = None

    # Channel
    channel:       str | None = None   # Human-readable (e.g. "WhatsApp")
    channel_raw:   str | None = None   # Raw code (e.g. "whatsapp")

    # Message content
    direction:     str | None = None   # "in" | "out"
    author_id:     int | None = None
    author_type:   str | None = None   # "user" | "contact" | "bot"
    author_name:   str | None = None
    message_text:  str | None = None
    media_url:     str | None = None

    # Timestamps
    timestamp:     int | None = None   # Unix timestamp
    timestamp_iso: str | None = None   # ISO 8601


# =============================================================================
# ExtractionResult
# =============================================================================

@dataclass
class ChatExtractionResult:
    """Summary returned by ChatsExtractor.extract_all()."""

    entity:            str   = "chats"
    total_chats:       int   = 0
    total_messages:    int   = 0
    failed_chats:      int   = 0
    failed_messages:   int   = 0
    inbound_messages:  int   = 0
    outbound_messages: int   = 0
    latest_message_ts: int | None = None
    output_path:       Path | None = None
    flat_output_path:  Path | None = None
    dead_letter_path:  Path | None = None
    duration_seconds:  float = 0.0
    started_at:        str   = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    finished_at:       str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity":            self.entity,
            "total_chats":       self.total_chats,
            "total_messages":    self.total_messages,
            "inbound":           self.inbound_messages,
            "outbound":          self.outbound_messages,
            "failed_chats":      self.failed_chats,
            "failed_messages":   self.failed_messages,
            "latest_message_ts": self.latest_message_ts,
            "output_path":       str(self.output_path),
            "flat_output_path":  str(self.flat_output_path),
            "duration_seconds":  round(self.duration_seconds, 2),
            "started_at":        self.started_at,
            "finished_at":       self.finished_at,
        }


# =============================================================================
# ChatsExtractor
# =============================================================================

class ChatsExtractor:
    """
    Extracts all chat conversations and messages from Kommo.

    Produces two output files:
      - chats.json        — one record per chat thread
      - messages_flat.json — one record per message, AI-ready flat schema

    The flat messages file is the primary input for Claude AI analysis.

    Args:
        client:        Open KommoAPIClient instance.
        output_dir:    Directory for output files.
        page_size:     Chats per API page (max 250).
        lead_names:    Optional dict {lead_id: lead_name} for enrichment.
        contact_names: Optional dict {lead_id: contact_name} for enrichment.
        extra_params:  Extra query parameters for the chats list request.

    Example:
        with KommoAPIClient(oauth) as client:
            extractor = ChatsExtractor(client, output_dir="outputs")
            result    = extractor.extract_all()
    """

    def __init__(
        self,
        client: KommoAPIClient,
        output_dir: str | Path = _DEFAULT_OUTPUT_DIR,
        page_size: int = _DEFAULT_PAGE_SIZE,
        lead_names:    dict[int, str] | None = None,
        contact_names: dict[int, str] | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        self._client        = client
        self._output_dir    = Path(output_dir)
        self._error_dir     = self._output_dir / "errors"
        self._page_size     = min(page_size, 250)
        self._lead_names    = lead_names or {}
        self._contact_names = contact_names or {}
        self._extra_params  = extra_params or {}

    # =========================================================================
    # PUBLIC: Main entry points
    # =========================================================================

    @retry_api_call
    def extract_all(self) -> ChatExtractionResult:
        """
        Extract all chat threads and their messages.

        Paginates /api/v4/chats, then fetches messages for each chat.
        Produces chats.json and messages_flat.json.

        Returns:
            ChatExtractionResult with counts, paths, and the latest
            message timestamp (use this to update StateManager cursor).

        Raises:
            KommoClientError: Unrecoverable API failure.
        """
        result  = ChatExtractionResult()
        started = time.monotonic()

        logger.info("Chat extraction started", extra={"page_size": self._page_size})

        all_chats:    list[dict[str, Any]] = []
        all_messages: list[dict[str, Any]] = []
        failed_chats: list[dict[str, Any]] = []
        latest_ts: int | None = None

        # ------------------------------------------------------------------
        # Step 1: Paginate all chat threads
        # ------------------------------------------------------------------
        try:
            for page_num, raw_page in enumerate(
                self._client.paginate(
                    path="/chats",
                    resource="chats",
                    page_size=self._page_size,
                    params=self._extra_params,
                ),
                start=1,
            ):
                for raw_chat in raw_page:
                    try:
                        from pydantic import ValidationError
                        chat = ChatRecord.model_validate(raw_chat)
                        all_chats.append(chat.model_dump(mode="json"))

                        # Step 2: Fetch messages for this chat
                        msgs, failed_msgs = self._fetch_messages(chat)
                        all_messages.extend(msgs)
                        result.failed_messages += len(failed_msgs)

                        # Track latest message timestamp for sync cursor
                        if chat.last_message_at:
                            if latest_ts is None or chat.last_message_at > latest_ts:
                                latest_ts = chat.last_message_at

                    except Exception as exc:
                        chat_id = raw_chat.get("id", "unknown")
                        logger.warning(
                            "Chat validation failed",
                            extra={"chat_id": chat_id, "error": str(exc)},
                        )
                        failed_chats.append({"_raw": raw_chat, "_error": str(exc)})

                logger.info(
                    "Chats page processed",
                    extra={
                        "page":          page_num,
                        "chats_this_page": len(raw_page),
                        "messages_total": len(all_messages),
                    },
                )

        except KommoNotFoundError:
            logger.info("No chats found in account (404 from API)")

        except KommoClientError as exc:
            logger.error(
                "Chat extraction failed",
                extra={"error": str(exc), "chats_collected": len(all_chats)},
            )
            raise

        # ------------------------------------------------------------------
        # Step 3: Build flat messages (AI-ready schema)
        # ------------------------------------------------------------------
        flat_messages = self._build_flat_messages(all_messages, all_chats)

        # Count direction stats
        for msg in all_messages:
            if msg.get("direction") == "in":
                result.inbound_messages += 1
            elif msg.get("direction") == "out":
                result.outbound_messages += 1

        # ------------------------------------------------------------------
        # Step 4: Persist outputs
        # ------------------------------------------------------------------
        result.total_chats      = len(all_chats)
        result.total_messages   = len(all_messages)
        result.failed_chats     = len(failed_chats)
        result.latest_message_ts = latest_ts

        if all_chats:
            result.output_path = self._write_json(
                "chats.json", all_chats,
                meta_extras={"total_messages": len(all_messages)},
            )

        if flat_messages:
            result.flat_output_path = self._write_json(
                "messages_flat.json", flat_messages,
                entity_name="messages",
                meta_extras={
                    "schema": "lead_id,lead_name,contact_name,channel,direction,author,message_text,timestamp",
                    "inbound":  result.inbound_messages,
                    "outbound": result.outbound_messages,
                    "note": "AI-ready flat schema — primary input for Claude analysis",
                },
            )

        if failed_chats:
            result.dead_letter_path = self._write_dead_letter(failed_chats)

        result.duration_seconds = time.monotonic() - started
        result.finished_at      = datetime.now(tz=timezone.utc).isoformat()

        logger.info("Chat extraction complete", extra=result.as_dict())
        return result

    def extract_since(self, last_message_timestamp: int) -> ChatExtractionResult:
        """
        Extract only chats with messages since the given timestamp.

        This is the primary incremental sync method for the Chats API.
        Uses Kommo's filter[last_message_at][from] parameter.

        Args:
            last_message_timestamp: Unix timestamp of the last processed message.

        Returns:
            ChatExtractionResult containing only new/updated conversations.
        """
        original = dict(self._extra_params)
        self._extra_params["filter[last_message_at][from]"] = last_message_timestamp
        try:
            return self.extract_all()
        finally:
            self._extra_params = original

    # =========================================================================
    # PRIVATE: Message fetching
    # =========================================================================

    def _fetch_messages(
        self,
        chat: ChatRecord,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Fetch all messages for a single chat thread.

        Args:
            chat: Validated ChatRecord whose messages to fetch.

        Returns:
            (valid_messages, failed_messages) as serialisable dicts.
        """
        from pydantic import ValidationError

        valid:  list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        try:
            for page in self._client.paginate(
                path=f"/chats/{chat.id}/messages",
                resource="messages",
                page_size=self._page_size,
            ):
                for raw_msg in page:
                    try:
                        # Inject chat_id if not present in message body
                        if "chat_id" not in raw_msg:
                            raw_msg["chat_id"] = chat.id
                        msg = MessageRecord.model_validate(raw_msg)
                        d   = msg.model_dump(mode="json")
                        # Carry forward chat context
                        d["entity_id"]    = chat.entity_id
                        d["entity_type"]  = chat.entity_type
                        d["channel_type"] = chat.channel_type
                        valid.append(d)
                    except (ValidationError, Exception) as exc:
                        failed.append({"_raw": raw_msg, "_error": str(exc)})

        except KommoNotFoundError:
            logger.debug("No messages for chat %s", chat.id)
        except KommoClientError as exc:
            logger.warning(
                "Failed to fetch messages for chat",
                extra={"chat_id": chat.id, "error": str(exc)},
            )

        return valid, failed

    # =========================================================================
    # PRIVATE: Flat message builder
    # =========================================================================

    def _build_flat_messages(
        self,
        messages: list[dict[str, Any]],
        chats: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Build AI-ready flat message records joining message + chat context.

        Each record contains the full context needed for Claude to analyse
        the conversation without joining multiple files.
        """
        # Build chat lookup: chat_id → chat dict
        chat_map = {c["id"]: c for c in chats}

        flat: list[dict[str, Any]] = []
        for msg in messages:
            chat    = chat_map.get(msg.get("chat_id", ""), {})
            lead_id = chat.get("entity_id") or msg.get("entity_id")

            author     = msg.get("author") or {}
            author_name = (
                author.get("name")
                if isinstance(author, dict)
                else None
            )
            channel_raw = chat.get("channel_type") or msg.get("channel_type") or msg.get("origin")
            channel     = _CHANNEL_LABELS.get(channel_raw or "", channel_raw or "Unknown")

            flat.append({
                # Spec fields
                "lead_id":       lead_id,
                "lead_name":     self._lead_names.get(lead_id) if lead_id else None,
                "contact_name":  self._contact_names.get(lead_id) if lead_id else None,
                "channel":       channel,
                "direction":     msg.get("direction"),
                "author":        author_name,
                "message_text":  msg.get("body"),
                "timestamp":     msg.get("created_at"),
                "timestamp_iso": msg.get("created_at_iso"),
                # Extended fields (useful for filtering/debugging)
                "message_id":   msg.get("id"),
                "chat_id":      msg.get("chat_id"),
                "channel_raw":  channel_raw,
                "author_type":  author.get("type") if isinstance(author, dict) else None,
                "author_id":    author.get("id") if isinstance(author, dict) else None,
                "media_url":    msg.get("media_url"),
            })

        # Sort chronologically
        flat.sort(key=lambda m: m.get("timestamp") or 0)
        return flat

    # =========================================================================
    # PRIVATE: File I/O
    # =========================================================================

    def _write_json(
        self,
        filename: str,
        records: list[dict[str, Any]],
        entity_name: str = "chats",
        meta_extras: dict[str, Any] | None = None,
    ) -> Path:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        path = self._output_dir / filename

        envelope: dict[str, Any] = {
            "_meta": {
                "entity":       entity_name,
                "count":        len(records),
                "extracted_at": datetime.now(tz=timezone.utc).isoformat(),
                "source":       "kommo_api_v4",
                **(meta_extras or {}),
            },
            "data": records,
        }
        self._atomic_write(path, envelope)
        logger.info("Written to disk", extra={"path": str(path), "count": len(records)})
        return path

    def _write_dead_letter(self, failed: list[dict[str, Any]]) -> Path:
        self._error_dir.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = self._error_dir / f"chats_failed_{ts}.json"
        self._atomic_write(path, {"_meta": {"entity": "chats", "type": "dead_letter"}, "data": failed})
        return path

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
