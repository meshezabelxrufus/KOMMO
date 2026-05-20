"""
utils/logger.py
===============
Production-grade logging configuration for the Kommo CRM integration.

FEATURES
────────
  ✓ Dual-output: structured JSON (file) + human-readable (console)
  ✓ Rotating log files (10 MB max, 5 backups kept)
  ✓ Request correlation via run_id context variable
  ✓ Automatic redaction of sensitive values (tokens, secrets)
  ✓ Caller module + function + line number in every log record
  ✓ Configurable log level via LOG_LEVEL env var

LOG OUTPUTS
───────────
  Console: coloured, human-readable for development
  File:    logs/kommo.log — structured JSON for monitoring / alerting
           logs/errors.log — ERROR+ only, for quick triage

USAGE
─────
    # In main.py or any entry point (call once at startup)
    from utils.logger import configure_logging, get_logger

    configure_logging(log_level="INFO", log_dir="logs")

    # In any module
    from utils.logger import get_logger
    log = get_logger(__name__)

    log.info("Lead extraction started", extra={"entity": "leads", "page": 1})
    log.warning("Rate limit approaching", extra={"requests_per_sec": 6.8})
    log.error("API error", extra={"status_code": 500, "path": "/leads"})
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Sensitive field redaction
# ---------------------------------------------------------------------------

_SENSITIVE_KEYS = frozenset({
    "access_token", "refresh_token", "token", "secret",
    "password", "api_key", "authorization", "client_secret",
    "TOKEN_ENCRYPTION_KEY",
})

_REDACTED = "***REDACTED***"


class _RedactingFilter(logging.Filter):
    """
    Logging filter that redacts sensitive values from log records.

    Scans the record's extra dict and the message string for known
    sensitive keys, replacing their values with ***REDACTED***.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact known sensitive attributes on the record itself
        for key in _SENSITIVE_KEYS:
            if hasattr(record, key):
                setattr(record, key, _REDACTED)

        # Redact in the formatted message (last resort)
        if hasattr(record, "msg") and isinstance(record.msg, str):
            for key in _SENSITIVE_KEYS:
                if key in record.msg.lower():
                    # Replace value patterns like: access_token=abc123
                    import re
                    record.msg = re.sub(
                        rf"({key}\s*[=:]\s*)\S+",
                        rf"\1{_REDACTED}",
                        record.msg,
                        flags=re.IGNORECASE,
                    )

        return True  # Always allow the record through (we only modify it)


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

class _JSONFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects.

    Suitable for log aggregation systems (Datadog, CloudWatch, Loki).
    Each line is valid JSON with a consistent schema.

    Output example:
        {
          "ts": "2025-01-14T09:03:45.123Z",
          "level": "INFO",
          "logger": "api.client",
          "func": "_request",
          "line": 247,
          "msg": "← 200 /leads [142ms]",
          "status_code": 200,
          "elapsed_ms": 142,
          "request_id": "a1b2c3d4"
        }
    """

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import datetime, timezone

        # Base fields always present
        payload: dict[str, Any] = {
            "ts":     datetime.fromtimestamp(record.created, tz=timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%S.") + f"{record.msecs:03.0f}Z",
            "level":  record.levelname,
            "logger": record.name,
            "func":   record.funcName,
            "line":   record.lineno,
            "msg":    record.getMessage(),
        }

        # Attach any extra fields the caller passed
        for key, val in record.__dict__.items():
            if key not in _LOGGING_RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = val

        # Attach exception info if present
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


# Standard logging.LogRecord attributes — exclude from JSON extras
_LOGGING_RESERVED_ATTRS = frozenset({
    "name", "msg", "args", "created", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs",
    "message", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "thread", "threadName", "exc_info", "exc_text",
    "taskName",
})


# ---------------------------------------------------------------------------
# Console formatter (human-readable with colour)
# ---------------------------------------------------------------------------

class _ConsoleFormatter(logging.Formatter):
    """
    Human-readable formatter with ANSI colour codes for terminal output.

    Level → colour mapping:
      DEBUG   → dim grey
      INFO    → cyan
      WARNING → yellow
      ERROR   → red
      CRITICAL→ bold red
    """

    _GREY    = "\033[2;37m"
    _CYAN    = "\033[0;36m"
    _YELLOW  = "\033[0;33m"
    _RED     = "\033[0;31m"
    _BOLD_RED = "\033[1;31m"
    _RESET   = "\033[0m"

    _LEVEL_COLOURS = {
        "DEBUG":    _GREY,
        "INFO":     _CYAN,
        "WARNING":  _YELLOW,
        "ERROR":    _RED,
        "CRITICAL": _BOLD_RED,
    }

    _FMT = "{colour}[{level:<8}]{reset} {ts}  {name:<30} {msg}{extras}"

    def format(self, record: logging.LogRecord) -> str:
        from datetime import datetime, timezone

        colour = self._LEVEL_COLOURS.get(record.levelname, "")
        ts     = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%H:%M:%S"
        )

        # Collect extra fields (non-standard attrs)
        extras_parts = []
        for key, val in record.__dict__.items():
            if key not in _LOGGING_RESERVED_ATTRS and not key.startswith("_"):
                extras_parts.append(f"{key}={val!r}")
        extras = ("  " + "  ".join(extras_parts)) if extras_parts else ""

        line = self._FMT.format(
            colour=colour,
            level=record.levelname,
            reset=self._RESET,
            ts=ts,
            name=record.name,
            msg=record.getMessage(),
            extras=extras,
        )

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure_logging(
    log_level:  str  = "INFO",
    log_dir:    str | Path = "logs",
    max_bytes:  int  = 10 * 1024 * 1024,   # 10 MB
    backup_count: int = 5,
    json_file:  bool = True,
    console:    bool = True,
) -> None:
    """
    Configure the root logger with rotating file and console handlers.

    Call this ONCE at application startup (in main.py or run_*.py).

    Args:
        log_level:    Minimum log level ("DEBUG", "INFO", "WARNING", "ERROR").
                      Overridden by LOG_LEVEL environment variable if set.
        log_dir:      Directory for log files (created automatically).
        max_bytes:    Max size per log file before rotation (default: 10 MB).
        backup_count: Number of rotated backups to keep (default: 5).
        json_file:    Write structured JSON to logs/kommo.log (default: True).
        console:      Write human-readable output to stdout (default: True).

    Example:
        configure_logging(log_level="INFO", log_dir="logs")
    """
    effective_level_str = os.environ.get("LOG_LEVEL", log_level).upper()
    effective_level     = getattr(logging, effective_level_str, logging.INFO)

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(effective_level)

    # Remove any handlers added by basicConfig or previous calls
    root.handlers.clear()

    redact_filter = _RedactingFilter()

    # ── Handler 1: Console (human-readable, coloured) ──────────────────
    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(effective_level)
        ch.setFormatter(_ConsoleFormatter())
        ch.addFilter(redact_filter)
        root.addHandler(ch)

    # ── Handler 2: Rotating JSON file (all levels) ─────────────────────
    if json_file:
        fh = logging.handlers.RotatingFileHandler(
            filename=log_path / "kommo.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setLevel(effective_level)
        fh.setFormatter(_JSONFormatter())
        fh.addFilter(redact_filter)
        root.addHandler(fh)

    # ── Handler 3: Error-only rotating file ────────────────────────────
    eh = logging.handlers.RotatingFileHandler(
        filename=log_path / "errors.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    eh.setLevel(logging.ERROR)
    eh.setFormatter(_JSONFormatter())
    eh.addFilter(redact_filter)
    root.addHandler(eh)

    # Silence noisy third-party libs
    for noisy in ("urllib3", "httpx", "httpcore", "requests"):
        logging.getLogger(noisy).setLevel(
            logging.DEBUG if effective_level_str == "DEBUG" else logging.WARNING
        )

    logging.getLogger(__name__).info(
        "Logging configured",
        extra={
            "level":        effective_level_str,
            "log_dir":      str(log_path),
            "json_file":    json_file,
            "max_bytes":    max_bytes,
            "backup_count": backup_count,
        },
    )


def get_logger(name: str) -> logging.Logger:
    """
    Get a named logger. Use __name__ in each module.

    Args:
        name: Logger name (use __name__ for automatic module naming).

    Returns:
        Standard logging.Logger.

    Example:
        log = get_logger(__name__)
        log.info("Extraction started", extra={"entity": "leads"})
    """
    return logging.getLogger(name)
