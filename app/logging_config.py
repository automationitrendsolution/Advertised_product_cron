"""Logging setup.

Two sinks: a console stream and a rotating file under ``LOG_DIR``, both in
the same plain layout, written for whoever reads the log rather than for the
code that wrote it::

    2026-10-30 11:02:34 IST | INFO  | Download | Sponsored Brands downloaded (9.0 KB)

Times are IST, the zone the schedule is written in, whatever the server's own
clock is set to.  Module names, line numbers and process ids appear only at
DEBUG.  A redaction filter is installed on both sinks so that a stray
``repr()`` of a DSN or a cookie jar can never reach disk.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOGGER_NAME = "advertised-product"

_LOG_TZ = ZoneInfo("Asia/Kolkata")

#: Logger name -> the stage of the job it reports on, as shown in the log.
_AREAS = {
    "pipeline": "Run",
    "cli": "Run",
    "runlock": "Run",
    "browser": "Browser",
    "auth": "Sign-in",
    "reports_page": "Amazon",
    "selectors": "Amazon",
    "downloader": "Download",
    "processor": "Process",
    "database": "Database",
    "retry": "Retry",
    "maintenance": "Cleanup",
}
_AREA_WIDTH = max(len(area) for area in _AREAS.values())

_LEVELS = {"WARNING": "WARN", "CRITICAL": "FATAL"}

#: Patterns scrubbed from every log record.  Each must keep group 1 as the
#: literal prefix so the replacement stays readable.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)"), r"\1***\2"),
    (
        re.compile(
            r"((?:password|passwd|pwd|secret|token|cookie|session[_-]?id|api[_-]?key|authorization)"
            r"\s*[=:]\s*)(?!\*)[^\s,;)'\"]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    # Token-bearing URL query parameters, e.g. Amazon's sign-in ssoResponse.
    (
        re.compile(
            r"([?&](?:ssoResponse|access_token|id_token|refresh_token|code|sig|signature)=)"
            r"[^&\s'\"]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
)


class RedactingFilter(logging.Filter):
    """Masks credential-shaped substrings in messages and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _redact(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_redact(a) for a in record.args)
        return True


def _redact(value: object) -> object:
    if not isinstance(value, str):
        return value
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


class ReadableFormatter(logging.Formatter):
    """``<IST time> | <level> | <stage> | <message>``.

    With ``detailed`` (DEBUG runs) the module and line are appended, for
    whoever is debugging the code rather than reading the run.
    """

    def __init__(self, *, detailed: bool = False) -> None:
        super().__init__()
        self.detailed = detailed

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, _LOG_TZ).strftime("%Y-%m-%d %H:%M:%S IST")
        level = _LEVELS.get(record.levelname, record.levelname)
        area = _AREAS.get(record.name.rsplit(".", 1)[-1], "Run")
        line = f"{stamp} | {level:<5} | {area:<{_AREA_WIDTH}} | {record.getMessage()}"
        if self.detailed:
            line += f"  [{record.module}:{record.lineno}]"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(
    log_dir: Path,
    *,
    level: str = "INFO",
    run_id: str | None = None,
    retention_days: int = 7,
    console: bool = True,
) -> logging.Logger:
    """Configure and return the ``advertised-product`` logger.

    Safe to call more than once; existing handlers are replaced so repeated
    calls (tests, re-entry) do not duplicate output.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    redactor = RedactingFilter()
    formatter = ReadableFormatter(detailed=logger.level <= logging.DEBUG)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        logger.addHandler(stream)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "adproduct.log",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    logger.addHandler(file_handler)

    # Third-party libraries are noisy at DEBUG and can echo connection strings.
    for noisy in ("sqlalchemy.engine", "mysql.connector", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if run_id:
        logger.debug("run_id=%s", run_id)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the ``advertised-product`` logger."""
    return logging.getLogger(LOGGER_NAME if not name else f"{LOGGER_NAME}.{name}")
