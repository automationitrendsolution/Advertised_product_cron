"""Wait for requested reports to finish, then download them.

"Run report" only *queues* a report; Amazon builds it in the background and
the download icon appears in its list row when it is done.  So the run polls:
reload the list, look up each pending report by its unique name, and download
any that are ready.  All three are queued before the first poll, so Amazon
builds them in parallel.

The download is captured through Playwright's ``download`` event rather than
by watching a directory, so a file is accepted only once Chromium reports the
transfer finished -- no partial ``.crdownload`` reads.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from playwright.sync_api import Download, Locator, Page

from .browser import BrowserSession
from .config import Settings
from .exceptions import DownloadError, ReportValidationError
from .logging_config import get_logger
from .reports_page import ReportsPage
from .schema import ReportSpec
from .timeutils import now_utc

logger = get_logger("downloader")

#: Anything smaller than this is a truncated or error response, not a report.
MIN_FILE_BYTES = 16
_HTML_PREFIXES = (b"<!doctype", b"<html", b"<?xml")
_ZIP_MAGIC = b"PK\x03\x04"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
#: Row text that means Amazon gave up building the report.
_FAILED = re.compile(r"\bfail(ed|ure)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class DownloadResult:
    """A validated report file on local disk."""

    spec_key: str
    report_name: str
    path: Path
    size_bytes: int
    suggested_filename: str

    @property
    def size_kb(self) -> float:
        return self.size_bytes / 1024


class ReportFailed(DownloadError):
    """Amazon marked a requested report as failed.  A new one must be created."""

    def __init__(self, spec_key: str, message: str) -> None:
        super().__init__(message)
        self.spec_key = spec_key


def wait_and_download(
    session: BrowserSession,
    reports: ReportsPage,
    settings: Settings,
    pending: dict[str, tuple[ReportSpec, str, str | None]],
    *,
    on_downloaded: Callable[[DownloadResult], None],
) -> None:
    """Poll until every pending report is downloaded.

    Each report is checked on its own History page (the URL Amazon opened
    after "Run report"), whose latest row goes Pending -> done with a
    download in its Action column.  A report without a known History page
    is looked up in the list by name instead.

    Args:
        pending: ``spec key -> (spec, unique report name, history URL)``.
            Entries are removed as they download, so on a retry the caller's
            dict holds only what is still outstanding.
        on_downloaded: Called with each finished file as soon as it lands,
            so a later failure does not lose earlier downloads.

    Raises:
        ReportFailed: Amazon marked a report as failed.
        DownloadError: A report was not ready within REPORT_READY_TIMEOUT_SECONDS.
    """
    deadline = monotonic() + settings.report_ready_timeout_seconds
    poll = 0
    while pending:
        poll += 1
        on_list = False
        for key, (spec, name, history_url) in list(pending.items()):
            if history_url:
                row = reports.open_history(history_url)
                on_list = False
            else:
                if not on_list:
                    reports.goto_list()
                    reports.wait_for_list()
                    on_list = True
                row = reports.find_report_row(name)
            if row is None:
                logger.info("%s: not listed yet (poll %d)", spec.label, poll)
                continue
            row_text = _row_text(row)
            if _FAILED.search(row_text.replace(name, "")):
                session.capture_failure(f"report-failed-{key}")
                raise ReportFailed(key, f"Amazon reports {name!r} as failed: {row_text[:200]}")
            button = reports.download_button(row)
            if button is None:
                logger.info(
                    "%s: still being built (poll %d): %s", spec.label, poll, row_text[:80]
                )
                continue
            result = _download(session, button, settings, key, name)
            pending.pop(key)
            on_downloaded(result)

        if not pending:
            break
        if monotonic() >= deadline:
            session.capture_failure("reports-not-ready")
            raise DownloadError(
                "Report(s) still not ready after "
                f"{settings.report_ready_timeout_seconds}s: "
                + ", ".join(name for _spec, name in pending.values())
                + ". Raise REPORT_READY_TIMEOUT_SECONDS if Amazon is slow today."
            )
        session.page.wait_for_timeout(settings.report_poll_seconds * 1000)


def _row_text(row: Locator) -> str:
    try:
        return re.sub(r"\s+", " ", row.inner_text(timeout=3_000)).strip()
    except Exception:
        return ""


def _download(
    session: BrowserSession, button: Locator, settings: Settings, key: str, name: str
) -> DownloadResult:
    """Click a row's download icon and save the file under a run-owned name."""
    download = _click_for_download(session, button, settings)
    failure = download.failure()
    if failure:
        session.capture_failure(f"download-failed-{key}")
        raise DownloadError(f"Download of {name!r} failed: {failure}")

    suggested = download.suggested_filename or f"{key}_report"
    target = settings.download_dir / _target_filename(key, suggested)
    try:
        download.save_as(str(target))
    except Exception as exc:
        raise DownloadError(f"Could not save {name!r} to {target}: {exc}") from exc

    target, size = validate_download(target)
    logger.info("Downloaded %s -> %s (%.1f KB)", name, target.name, size / 1024)
    return DownloadResult(key, name, target, size, suggested)


def _click_for_download(
    session: BrowserSession, button: Locator, settings: Settings
) -> Download:
    """Click and return the browser's download, wherever it starts.

    Amazon may download in place or via a short-lived tab, so both the page
    and any page it opens are listened to.
    """
    page = session.page
    context = page.context
    started: list[Download] = []
    opened: list[Page] = []

    # Real functions, not ``started.append``: Playwright tags the handler
    # with an attribute, which a builtin method cannot carry.
    def on_download(download: Download) -> None:
        started.append(download)

    def on_page(new_page: Page) -> None:
        opened.append(new_page)
        new_page.on("download", on_download)

    page.on("download", on_download)
    context.on("page", on_page)
    try:
        button.click()
        deadline = monotonic() + settings.download_timeout_ms / 1000
        while not started and monotonic() < deadline:
            page.wait_for_timeout(250)
    finally:
        page.remove_listener("download", on_download)
        context.remove_listener("page", on_page)

    if not started:
        session.capture_failure("download-timeout")
        raise DownloadError(
            f"Clicked download, but no file arrived within {settings.download_timeout_ms} ms. "
            "Raise DOWNLOAD_TIMEOUT_MS if this persists."
        )
    download = started[0]
    download.path()  # blocks until the transfer is complete
    for extra in opened:
        try:
            extra.close()
        except Exception:
            pass
    return download


def _target_filename(key: str, suggested: str) -> str:
    """``<UTC stamp>_<key>_<Amazon's name>``: owned by housekeeping, never colliding."""
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    safe = _SAFE_NAME.sub("_", Path(suggested).name).strip("_") or "report"
    return f"{stamp}_{key}_{safe}"


def validate_download(path: Path) -> tuple[Path, int]:
    """Check ``path`` is a plausible CSV/XLSX report; fix its extension.

    Returns the (possibly renamed) path and its size.

    Raises:
        ReportValidationError: Missing, too small, or an HTML page.
    """
    if not path.is_file():
        raise ReportValidationError(f"Downloaded file does not exist: {path}")
    size = path.stat().st_size
    if size < MIN_FILE_BYTES:
        raise ReportValidationError(f"Downloaded file {path} is only {size} bytes")
    with path.open("rb") as handle:
        head = handle.read(512)
    if head.lstrip().lower().startswith(_HTML_PREFIXES):
        raise ReportValidationError(
            f"Downloaded file {path} is an HTML page, not a report. This usually "
            "means the session expired mid-download."
        )

    # Housekeeping only deletes .csv/.xlsx/.png, so the extension must match.
    wanted = ".xlsx" if head.startswith(_ZIP_MAGIC) else ".csv"
    if path.suffix.lower() != wanted:
        renamed = path.with_name(path.stem + wanted if path.suffix.lower() in {".csv", ".xlsx"}
                                 else path.name + wanted)
        path.rename(renamed)
        path = renamed
    return path, size
