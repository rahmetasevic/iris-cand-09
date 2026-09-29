"""Single-line ``key=value`` logging to stdout (container friendly)."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from typing import Any


def fields(**values: Any) -> dict[str, Any]:
    """Structured context for a log call: ``log.info("msg", extra=fields(a=1))``."""
    return {"fields": values}


def _quote(value: Any) -> str:
    text = str(value)
    if text == "" or any(c in text for c in ' "=\n\t'):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return text


class LogfmtFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")
        parts = [
            f"ts={ts}",
            f"level={record.levelname.lower()}",
            f"logger={record.name}",
            f"msg={_quote(record.getMessage())}",
        ]
        for key, value in getattr(record, "fields", {}).items():
            parts.append(f"{key}={_quote(value)}")
        if record.exc_info:
            parts.append(f"error={_quote(self.formatException(record.exc_info))}")
        return " ".join(parts)


def configure(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(LogfmtFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("psycopg").setLevel(max(logging.getLevelName(level), logging.WARNING))
