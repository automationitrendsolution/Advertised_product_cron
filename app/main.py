"""Pipeline orchestration and execution modes.

    Reports page -> Playwright -> 3 x (Create report, Daily, Today|Yesterday)
        -> wait + download -> pandas -> MySQL ``Advertised_product``

Each stage raises its own exception type (see :mod:`app.exceptions`); this
module decides what that means for the run, what gets logged, and what the
process exits with.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import TypeVar

from .auth import sign_in, sign_in_interactive
from .browser import BrowserSession
from .config import Settings
from .database import AdvertisedProductRepository, WriteResult, build_repository
from .downloader import DownloadResult, ReportFailed, wait_and_download
from .exceptions import (
    AdProductError,
    AuthenticationError,
    DatabaseError,
    EmptyReportError,
    ReportValidationError,
)
from .logging_config import get_logger
from .maintenance import cleanup_old_files
from .processor import (
    ProcessedReport,
    combine,
    identify_report,
    read_report_file,
    to_records,
    transform_report,
)
from .reports_page import ReportsPage
from .retry import retry_call
from .runlock import run_lock
from .schema import REPORTS, Period, ReportSpec
from .timeutils import INDIA_TZ, US_PACIFIC_TZ, now_in, now_utc, resolve_report_date

logger = get_logger("pipeline")

T = TypeVar("T")


class RunMode(str, Enum):
    """What a single invocation is supposed to do."""

    FULL = "full"
    DOWNLOAD_ONLY = "download-only"
    PROCESS_FILES = "process-files"
    LOGIN = "login"


@dataclass
class RunOptions:
    """Per-invocation switches, all settable from the CLI."""

    mode: RunMode = RunMode.FULL
    period: Period = Period.TODAY
    dry_run: bool = False
    headed: bool | None = None
    files: list[Path] = field(default_factory=list)
    use_lock: bool = True
    echo_sql: bool = False

    @property
    def writes_database(self) -> bool:
        """Whether this run is allowed to modify MySQL."""
        return not self.dry_run and self.mode in {RunMode.FULL, RunMode.PROCESS_FILES}

    @property
    def label(self) -> str:
        label = self.mode.value
        if self.mode in {RunMode.FULL, RunMode.DOWNLOAD_ONLY}:
            label = f"{label} ({self.period.value})"
        return f"{label} dry-run" if self.dry_run else label


@dataclass
class RunSummary:
    """Everything the run is expected to report on."""

    mode: str
    started_at: object = field(default_factory=now_utc)
    finished_at: object | None = None
    expected_date: date | None = None
    downloads: list[DownloadResult] = field(default_factory=list)
    report_rows: dict[str, int] = field(default_factory=dict)
    data_dates: list[str] = field(default_factory=list)
    write_result: WriteResult | None = None
    error: str | None = None
    succeeded: bool = False

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or now_utc()
        return (end - self.started_at).total_seconds()  # type: ignore[operator]

    def log(self) -> None:
        logger.info("=" * 72)
        logger.info("Run summary (%s)", self.mode)
        logger.info("  started (UTC)      : %s", self.started_at)
        logger.info("  started (IST)      : %s", now_in(INDIA_TZ, reference=self.started_at))  # type: ignore[arg-type]
        logger.info("  duration           : %.1fs", self.duration_seconds)
        logger.info("  expected date      : %s", self.expected_date or "n/a")
        for download in self.downloads:
            logger.info(
                "  downloaded         : %s (%.1f KB)", download.path.name, download.size_kb
            )
        for label, rows in self.report_rows.items():
            logger.info("  %-18s : %d row(s)", label, rows)
        logger.info("  dates in data      : %s", ", ".join(self.data_dates) or "none")
        if self.write_result:
            logger.info(
                "  %s: %d deleted, %d inserted",
                self.write_result.table,
                self.write_result.deleted,
                self.write_result.inserted,
            )
        logger.info("  status             : %s", "SUCCESS" if self.succeeded else "FAILED")
        if self.error:
            logger.info("  error              : %s", self.error)
        logger.info("=" * 72)


class AdProductPipeline:
    """Runs one AdProduct execution end to end."""

    def __init__(
        self,
        settings: Settings,
        options: RunOptions,
        *,
        repository_factory: Callable[[], AdvertisedProductRepository] | None = None,
    ) -> None:
        """
        Args:
            repository_factory: Overrides how the MySQL repository is built.
                Used by tests to substitute a local database; production always
                leaves this None.
        """
        self.settings = settings
        self.options = options
        self.summary = RunSummary(mode=options.label)
        # A caller that supplies its own repository owns its lifecycle, so the
        # pipeline must not dispose the engine out from under it.
        self._owns_repository = repository_factory is None
        self._repository_factory = repository_factory or (
            lambda: build_repository(settings, echo=options.echo_sql)
        )

    # -- entry point -------------------------------------------------------

    def run(self) -> RunSummary:
        """Execute the configured mode, returning the summary either way."""
        self.settings.ensure_directories()
        self._log_start()
        try:
            with run_lock(
                self.settings.lock_file,
                enabled=self.options.use_lock,
                wait_seconds=self.settings.lock_wait_seconds,
            ):
                if self.options.mode is RunMode.LOGIN:
                    self._run_login()
                elif self.options.mode is RunMode.DOWNLOAD_ONLY:
                    self._run_download_only()
                elif self.options.mode is RunMode.PROCESS_FILES:
                    self._run_process_files()
                else:
                    self._run_full()
            self.summary.succeeded = True
        except AdProductError as exc:
            self.summary.error = f"{type(exc).__name__}: {exc}"
            logger.error("Run failed: %s", self.summary.error)
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced after summary
            self.summary.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Unexpected failure")
            raise
        finally:
            self.summary.finished_at = now_utc()
            self.summary.log()
        return self.summary

    def _log_start(self) -> None:
        logger.info("=" * 72)
        logger.info("AdProduct starting | mode=%s", self.options.label)
        logger.info("  UTC            : %s", now_utc().isoformat())
        logger.info("  US/Pacific     : %s", now_in(US_PACIFIC_TZ).isoformat())
        logger.info("  Asia/Kolkata   : %s", now_in(INDIA_TZ).isoformat())
        for key, value in self.settings.describe().items():
            logger.info("  %-14s : %s", key, value)
        logger.info("=" * 72)

    def resolve_expected_date(self) -> date:
        expected = resolve_report_date(
            self.settings.report_timezone, offset_days=self.options.period.offset_days
        )
        self.summary.expected_date = expected
        logger.info(
            "Period %s -> expecting data for %s (%s)",
            self.options.period.preset,
            expected,
            self.settings.report_timezone,
        )
        return expected

    # -- modes -------------------------------------------------------------

    def _run_full(self) -> None:
        expected = self.resolve_expected_date()
        repository = self._open_repository()
        downloads: dict[str, DownloadResult] = {}
        try:
            downloads = self._download_with_retry()
            files = {key: result.path for key, result in downloads.items()}
            self._process_and_load(files, repository, expected)
            self._dispose_files(list(files.values()))
        finally:
            # Housekeeping also has to run when a run fails -- otherwise a day
            # of failures prunes nothing -- but must never mask the error.
            try:
                cleanup_old_files(self.settings)
            except Exception:
                logger.warning("Housekeeping failed", exc_info=True)
            if repository is not None and self._owns_repository:
                repository.dispose()

    def _run_download_only(self) -> None:
        self.resolve_expected_date()
        downloads = self._download_with_retry()
        for result in downloads.values():
            logger.info("Download-only: %s (%.1f KB)", result.path, result.size_kb)

    def _run_process_files(self) -> None:
        """Load files downloaded by hand, recognising each by its columns."""
        files: dict[str, Path] = {}
        for raw in self.options.files:
            path = Path(raw).expanduser().resolve()
            frame = read_report_file(path)
            if frame.empty:
                raise ReportValidationError(
                    f"{path.name} has no rows, so its report type cannot be recognised. "
                    "Process-files needs one non-empty file per report."
                )
            spec = identify_report(frame)
            if spec.key in files:
                raise ReportValidationError(
                    f"Two files are both {spec.label}: {files[spec.key].name}, {path.name}"
                )
            logger.info("%s is the %s report", path.name, spec.label)
            files[spec.key] = path

        missing = [spec.label for spec in REPORTS if spec.key not in files]
        if missing:
            # The load deletes whole dates, so loading only some reports would
            # silently drop the others' rows for those dates.
            raise ReportValidationError(
                "All three reports are needed, because the load replaces whole dates. "
                f"Missing: {', '.join(missing)}"
            )

        repository = self._open_repository()
        try:
            self._process_and_load(files, repository, expected=None)
        finally:
            if repository is not None and self._owns_repository:
                repository.dispose()

    def _run_login(self) -> None:
        """Establish the Amazon session with a person at the keyboard.

        Headed when there is a display (or ``--headed``): the person finishes
        whatever Amazon asks on screen.  Otherwise headless, with Amazon's
        two-step code typed at the terminal prompt.  Either way Chromium saves
        the session in ``SESSION_DIR`` for the scheduled runs.
        """
        headless = not self._can_show_a_window()
        if headless:
            print("\n" + "=" * 72)
            print("Amazon sign-in (no display -- codes are typed here)")
            print("Chromium runs headless; you answer any two-step code at the prompt.")
            print("=" * 72)

        with BrowserSession(self.settings, headless=headless) as session:
            reports = ReportsPage(session, self.settings)
            reports.goto_list()

            if headless:
                if reports.is_showing_sign_in():
                    sign_in_interactive(session, self.settings)
                else:
                    print("\nAlready signed in -- the saved session is still good.")
                reports.goto_list()
            else:
                if self.settings.has_credentials and reports.is_showing_sign_in():
                    print("\nFilling in the configured credentials...")
                    try:
                        sign_in(session, self.settings)
                    except AuthenticationError as exc:
                        # Expected whenever Amazon wants a human: keep the
                        # browser open so they can finish by hand.
                        print(f"\nAmazon needs you to finish this by hand:\n  {exc}\n")
                print("\n" + "=" * 72)
                print("Finish anything Amazon still asks for (OTP / MFA / CAPTCHA) in the")
                print("browser window, until the Sponsored ads reports list is visible.")
                print("Then return here and press Enter.")
                print("=" * 72 + "\n")
                input("Press Enter once you can see the reports list... ")

            print("\nChecking the session (this takes a few seconds)...")
            reports.assert_authenticated()
            print("  ✓ signed in -- no sign-in page or challenge on screen")
            reports.wait_for_list()
            print("  ✓ reports list found")

            logger.info("Session established and saved to %s", self.settings.session_dir)
            print(f"\nSession stored in {self.settings.session_dir}")
            print("Scheduled runs can use it now. Verify with:")
            print("    uv run python run.py --period today --dry-run\n")

    def _can_show_a_window(self) -> bool:
        """Whether a headed browser would actually be visible to anyone."""
        if self.options.headed:
            return True
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    # -- stages ------------------------------------------------------------

    def _open_repository(self) -> AdvertisedProductRepository | None:
        """Connect to MySQL first, so a bad DSN fails before any browser work.

        A dry run continues without the database when it is unreachable.
        """
        try:
            return self._retry("Database connection", self._repository_factory)
        except DatabaseError:
            if self.options.writes_database:
                raise
            logger.warning("DRY RUN: MySQL is unreachable; continuing without it")
            return None

    def _report_name(self, spec: ReportSpec) -> str:
        stamp = now_utc().strftime("%Y%m%d-%H%M%S")
        return (
            f"{self.settings.report_name_prefix} {spec.key.upper()} "
            f"{self.options.period.value} {stamp}"
        )

    def _download_with_retry(self) -> dict[str, DownloadResult]:
        """Create all three reports, then wait for and download each.

        State survives a retry: a report already downloaded is not fetched
        again, and one already requested is waited for rather than requested
        a second time.  Only a report Amazon marked *failed* is re-created.
        """
        downloaded: dict[str, DownloadResult] = {}
        requested: dict[str, tuple[ReportSpec, str]] = {}

        def keep(result: DownloadResult) -> None:
            downloaded[result.spec_key] = result
            self.summary.downloads.append(result)

        def attempt() -> None:
            with BrowserSession(self.settings, headless=self._headless()) as session:
                reports = ReportsPage(session, self.settings)
                reports.open()
                for spec in REPORTS:
                    if spec.key in downloaded or spec.key in requested:
                        continue
                    name = self._report_name(spec)
                    reports.create_report(spec, self.options.period, name)
                    requested[spec.key] = (spec, name)
                try:
                    wait_and_download(
                        session, reports, self.settings, requested, on_downloaded=keep
                    )
                except ReportFailed as exc:
                    requested.pop(exc.spec_key, None)
                    raise

        self._retry("Amazon report download", attempt)
        return downloaded

    def _headless(self) -> bool:
        if self.options.headed is None:
            return self.settings.headless
        return not self.options.headed

    def _process_and_load(
        self,
        files: dict[str, Path],
        repository: AdvertisedProductRepository | None,
        expected: date | None,
    ) -> None:
        processed: list[ProcessedReport] = []
        for spec in REPORTS:
            path = files[spec.key]
            frame = read_report_file(path)
            if not frame.empty and identify_report(frame) is not spec:
                # Catches the form having been left on the wrong report type.
                raise ReportValidationError(
                    f"{path.name} was requested as {spec.label} but its columns are "
                    f"those of {identify_report(frame).label}"
                )
            report = transform_report(
                frame, spec, source=path, date_format=self.settings.date_format
            )
            processed.append(report)
            self.summary.report_rows[spec.label] = report.row_count

        combined = combine(processed, date_column=self.settings.date_column)
        dates = sorted(combined[self.settings.date_column].dropna().unique().tolist())
        self.summary.data_dates = [str(value) for value in dates]
        if combined.empty:
            raise EmptyReportError(
                "All three reports are empty; nothing was deleted or inserted"
            )

        if expected is not None:
            expected_value = expected.strftime(self.settings.date_format)
            if self.summary.data_dates != [expected_value]:
                logger.warning(
                    "Expected data for %s only, but the reports hold %s. The dates in "
                    "the data are what gets replaced.",
                    expected_value,
                    ", ".join(self.summary.data_dates),
                )

        if repository is None or not self.options.writes_database:
            logger.info(
                "DRY RUN: %d row(s) prepared for %s; nothing written to MySQL",
                len(combined),
                ", ".join(self.summary.data_dates),
            )
            return

        records = to_records(combined)
        self.summary.write_result = self._retry(
            "Database load", lambda: repository.replace_dates(records)
        )

    def _retry(self, description: str, operation: Callable[[], T]) -> T:
        return retry_call(
            operation,
            description=description,
            attempts=self.settings.max_retries,
            base_delay=self.settings.retry_backoff_seconds,
            max_delay=self.settings.retry_backoff_max_seconds,
        )

    def _dispose_files(self, paths: list[Path]) -> None:
        """Delete (or archive) report files whose rows are now in MySQL.

        A dry run always archives, so the files can be inspected.
        """
        keep = self.settings.keep_processed_files or not self.options.writes_database
        for path in paths:
            try:
                if keep:
                    destination = self.settings.processed_dir / path.name
                    shutil.move(str(path), str(destination))
                    logger.info("Archived %s", destination)
                else:
                    path.unlink(missing_ok=True)
                    logger.info("Deleted %s now that it is loaded", path.name)
            except OSError as exc:
                # Not fatal: housekeeping removes it after FILE_RETENTION_DAYS.
                logger.warning("Could not dispose of %s: %s", path, exc)


def handle_failure(pipeline: AdProductPipeline, error: BaseException) -> None:
    """Emit the operator-facing guidance for a failed run."""
    if isinstance(error, AuthenticationError):
        logger.error(
            "Authentication is required and cannot be automated. "
            "Run 'python run.py --login' to refresh the session; without a display "
            "it asks for Amazon's two-step code in the terminal."
        )
    if isinstance(error, EmptyReportError):
        logger.error(
            "The reports contained no rows. Nothing was deleted or inserted; "
            "existing data is unchanged."
        )
