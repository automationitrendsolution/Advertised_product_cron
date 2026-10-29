"""Timezone-aware time handling.

Every timestamp in AdProduct is explicit about its zone.  The server's local
time is never used, so the same code produces the same report date on a server
in UTC, a laptop in IST, and a container with no ``/etc/localtime`` at all.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import INDIA_TZ, US_PACIFIC_TZ, UTC

__all__ = [
    "UTC",
    "INDIA_TZ",
    "US_PACIFIC_TZ",
    "now_utc",
    "now_in",
    "resolve_report_date",
]


def now_utc() -> datetime:
    """Current instant, in UTC."""
    return datetime.now(tz=UTC)


def now_in(tz: ZoneInfo, *, reference: datetime | None = None) -> datetime:
    """Convert ``reference`` (default: now) into ``tz``."""
    return (reference or now_utc()).astimezone(tz)


def resolve_report_date(
    report_timezone: ZoneInfo,
    *,
    offset_days: int = 0,
    reference: datetime | None = None,
) -> date:
    """Return the day Amazon's "Today" / "Yesterday" preset should cover.

    Amazon computes the presets in the marketplace's zone ("Dates are based on
    the campaign's country"), so "today" is defined in ``report_timezone`` --
    never in the server's zone.  ``offset_days`` is ``0`` for Today and ``-1``
    for Yesterday.  Used to check the downloaded rows, not to choose them.
    """
    local_now = now_in(report_timezone, reference=reference)
    return (local_now + timedelta(days=offset_days)).date()
