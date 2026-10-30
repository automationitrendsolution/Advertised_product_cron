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
import sys
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
    ConfigurationError,
    DatabaseError,
    DownloadError,
    EmptyReportError,
    LockError,
    NavigationError,
    ReportConfigError,
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

_RULE = "=" * 78
#: Width of the field names in the start and result blocks.
_FIELD = 10


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

    @property
    def description(self) -> str:
        """What this run does, in words for the log."""
        day = "today's" if self.period is Period.TODAY else "yesterday's"
        if self.mode is RunMode.LOGIN:
            text = "Sign in to Amazon (one-time setup)"
        elif self.mode is RunMode.PROCESS_FILES:
            text = "Save report files that were downloaded by hand"
        elif self.mode is RunMode.DOWNLOAD_ONLY:
            text = f"Download {day} 3 reports only (no database)"
        else:
            text = f"Download {day} 3 reports and save them to the database"
        if self.dry_run:
            text += " -- DRY RUN, nothing is saved"
        return text


@dataclass
class RunSummary:
    """Everything the run is expected to report on."""

    mode: str
    description: str = ""
    started_at: object = field(default_factory=now_utc)
    finished_at: object | None = None
    expected_date: date | None = None
    downloads: list[DownloadResult] = field(default_factory=list)
    report_rows: dict[str, int] = field(default_factory=dict)
    data_dates: list[str] = field(default_factory=list)
    write_result: WriteResult | None = None
    error: str | None = None
    advice: str | None = None
    no_data: bool = False
    dry_run: bool = False
    succeeded: bool = False

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or now_utc()
        return (end - self.started_at).total_seconds()  # type: ignore[operator]

    @property
    def headline(self) -> str:
        if self.no_data:
            return f"NO DATA YET - Amazon has no rows for {self.expected_date or 'this day'} yet"
        if not self.succeeded:
            return "FAILED"
        if self.write_result:
            return (
                f"SUCCESS - {self.write_result.inserted:,} rows saved for "
                f"{', '.join(self.write_result.dates)}"
            )
        if self.report_rows:
            total = sum(self.report_rows.values())
            return f"SUCCESS (dry run) - {total:,} rows ready, nothing saved"
        if self.downloads:
            return f"SUCCESS - {len(self.downloads)} report(s) downloaded"
        return "SUCCESS"

    def log(self) -> None:
        failed = not (self.succeeded or self.no_data)
        write = _block_writer(logger.error if failed else logger.info)
        logger.info(_RULE)
        write("RESULT", self.headline)
        logger.info(_RULE)
        write("What", self.description or self.mode)
        write("Started", _ist(self.started_at))
        write(
            "Finished",
            f"{_ist(self.finished_at or now_utc())}  (took {_duration(self.duration_seconds)})",
        )
        if self.expected_date:
            write("Data for", f"{self.expected_date} (Amazon's day, US Pacific time)")
        elif self.data_dates:
            write("Data for", ", ".join(self.data_dates))
        if self.report_rows:
            width = max(len(label) for label in self.report_rows)
            lines = [f"{label:<{width}}  {rows:>8,}" for label, rows in self.report_rows.items()]
            lines.append(f"{'Total':<{width}}  {sum(self.report_rows.values()):>8,}")
            write("Rows", *lines)
        elif self.downloads:
            write("Files", *(f"{d.path.name} ({d.size_kb:,.1f} KB)" for d in self.downloads))
        if self.write_result:
            write(
                "Database",
                f"{self.write_result.table}: removed {self.write_result.deleted:,} old rows, "
                f"saved {self.write_result.inserted:,} new rows",
            )
        elif self.no_data or failed:
            write("Database", "not changed (existing data is untouched)")
        elif self.dry_run:
            write("Database", "not changed (dry run)")
        if self.error and not self.no_data:
            write("Problem", self.error)
        if self.advice:
            write("What next", self.advice)
        logger.info(_RULE)


def _block_writer(emit: Callable[..., None]) -> Callable[..., None]:
    """``write("Field", "line", "more")`` -> aligned ``Field : line`` rows."""

    def write(name: str, *lines: str) -> None:
        for index, line in enumerate(lines):
            emit(" %-*s %s %s", _FIELD, name if index == 0 else "", ":" if index == 0 else " ", line)

    return write


def _ist(moment: object) -> str:
    return now_in(INDIA_TZ, reference=moment).strftime("%Y-%m-%d %H:%M IST")  # type: ignore[arg-type]


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def advice_for(error: BaseException) -> str:
    """What a person reading the log should do about ``error``."""
    if isinstance(error, EmptyReportError):
        return (
            "Nothing to do. This is normal just after midnight US time; "
            "a later run will pick the data up."
        )
    if isinstance(error, AuthenticationError):
        return "The Amazon sign-in has expired. Sign in again with: uv run python run.py --login"
    if isinstance(error, LockError):
        return "Another run was still going. The next scheduled run will try again."
    if isinstance(error, ConfigurationError):
        return "Check the settings in the .env file."
    if isinstance(error, DatabaseError):
        return (
            "Check that MySQL is reachable and the DB_* values in .env are right. "
            "Nothing was half-saved: the database changes all at once or not at all."
        )
    if isinstance(error, (DownloadError, NavigationError, ReportConfigError)):
        return (
            "Amazon was slow or its page changed. The next scheduled run will try again; "
            "if it keeps happening, look at the newest picture in data/screenshots."
        )
    if isinstance(error, ReportValidationError):
        return "A report file did not look right; see the problem above."
    return "Unexpected error; the details are in the lines above."


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
        self.summary = RunSummary(
            mode=options.label, description=options.description, dry_run=options.dry_run
        )
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
        if self.options.mode in {RunMode.FULL, RunMode.DOWNLOAD_ONLY}:
            self.resolve_expected_date()
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
        except EmptyReportError as exc:
            # Not a fault: Amazon simply has nothing for the day yet.
            self.summary.no_data = True
            self.summary.error = str(exc)
            self.summary.advice = advice_for(exc)
            raise
        except AdProductError as exc:
            self.summary.error = str(exc)
            self.summary.advice = advice_for(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced after summary
            self.summary.error = f"{type(exc).__name__}: {exc}"
            self.summary.advice = advice_for(exc)
            logger.exception("The run stopped on an unexpected error")
            raise
        finally:
            self.summary.finished_at = now_utc()
            self.summary.log()
        return self.summary

    def _log_start(self) -> None:
        write = _block_writer(logger.info)
        logger.info(_RULE)
        write("START", "Advertised product report")
        logger.info(_RULE)
        write("What", self.options.description)
        write(
            "Time",
            f"{_ist(self.summary.started_at)}  "
            f"(US Pacific: {now_in(US_PACIFIC_TZ).strftime('%Y-%m-%d %H:%M')})",
        )
        if self.summary.expected_date:
            write("Data for", f"{self.summary.expected_date} (Amazon's day, US Pacific time)")
        if self.options.mode is not RunMode.LOGIN:
            write("Database", f"{self.settings.database.name}.{self.settings.table}")
        logger.info(_RULE)
        # The full configuration is for debugging, not for reading every run.
        for key, value in self.settings.describe().items():
            logger.debug("setting %-20s : %s", key, value)

    def _step(self, number: int, total: int, text: str) -> None:
        logger.info("--- Step %d of %d: %s ---", number, total, text)

    def resolve_expected_date(self) -> date:
        expected = resolve_report_date(
            self.settings.report_timezone, offset_days=self.options.period.offset_days
        )
        self.summary.expected_date = expected
        logger.debug(
            "Period %s -> expecting data for %s (%s)",
            self.options.period.preset,
            expected,
            self.settings.report_timezone,
        )
        return expected

    # -- modes -------------------------------------------------------------

    def _run_full(self) -> None:
        expected = self.summary.expected_date or self.resolve_expected_date()
        self._step(1, 4, "Connect to the database")
        repository = self._open_repository()
        downloads: dict[str, DownloadResult] = {}
        try:
            downloads = self._download_with_retry(first_step=2, total_steps=4)
            files = {key: result.path for key, result in downloads.items()}
            self._step(
                4,
                4,
                "Read the reports and save them to the database"
                if self.options.writes_database
                else "Read the reports (dry run: not saving)",
            )
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
        downloads = self._download_with_retry(first_step=1, total_steps=2)
        for result in downloads.values():
            logger.info("Saved %s (%.1f KB)", result.path, result.size_kb)

    def _run_process_files(self) -> None:
        """Load files downloaded by hand, recognising each by its columns."""
        self._step(1, 3, "Check the files")
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
            logger.info("%s is the %s report", path.name, spec.category)
            files[spec.key] = path

        missing = [spec.label for spec in REPORTS if spec.key not in files]
        if missing:
            # The load deletes whole dates, so loading only some reports would
            # silently drop the others' rows for those dates.
            raise ReportValidationError(
                "All three reports are needed, because the load replaces whole dates. "
                f"Missing: {', '.join(missing)}"
            )

        self._step(2, 3, "Connect to the database")
        repository = self._open_repository()
        try:
            self._step(
                3,
                3,
                "Read the reports and save them to the database"
                if self.options.writes_database
                else "Read the reports (dry run: not saving)",
            )
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
        # Windows and macOS desktops always have one; Linux has one only when
        # an X11/Wayland display is set (never over plain SSH).
        if sys.platform in ("win32", "darwin"):
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
            logger.warning("Dry run: cannot reach MySQL, carrying on without it")
            return None

    def _report_name(self, spec: ReportSpec) -> str:
        stamp = now_utc().strftime("%Y%m%d-%H%M%S")
        return (
            f"{self.settings.report_name_prefix} {spec.key.upper()} "
            f"{self.options.period.value} {stamp}"
        )

    def _download_with_retry(
        self, *, first_step: int, total_steps: int
    ) -> dict[str, DownloadResult]:
        """Create all three reports, then wait for and download each.

        State survives a retry: a report already downloaded is not fetched
        again, and one already requested is waited for rather than requested
        a second time.  Only a report Amazon marked *failed* is re-created.
        """
        downloaded: dict[str, DownloadResult] = {}
        requested: dict[str, tuple[ReportSpec, str, str | None]] = {}

        def keep(result: DownloadResult) -> None:
            downloaded[result.spec_key] = result
            self.summary.downloads.append(result)

        def attempt() -> None:
            with BrowserSession(self.settings, headless=self._headless()) as session:
                reports = ReportsPage(session, self.settings)
                to_create = [
                    spec for spec in REPORTS
                    if spec.key not in downloaded and spec.key not in requested
                ]
                if to_create:
                    self._step(
                        first_step,
                        total_steps,
                        f"Ask Amazon for {len(to_create)} report(s) for "
                        f"{self.options.period.value}",
                    )
                reports.open()
                for spec in REPORTS:
                    if spec.key in downloaded or spec.key in requested:
                        continue
                    name = self._report_name(spec)
                    history_url = reports.create_report(spec, self.options.period, name)
                    requested[spec.key] = (spec, name, history_url)
                    # Back to the list for the next "Create report".
                    reports.goto_list()
                    reports.wait_for_list()
                self._step(
                    first_step + 1,
                    total_steps,
                    "Wait for Amazon to build the reports, then download them",
                )
                try:
                    wait_and_download(
                        session, reports, self.settings, requested, on_downloaded=keep
                    )
                except ReportFailed as exc:
                    requested.pop(exc.spec_key, None)
                    raise

        self._retry("Amazon download", attempt)
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
            self.summary.report_rows[spec.category] = report.row_count

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
                    "Expected data for %s only, but the reports hold %s. "
                    "Every date in the reports will be replaced.",
                    expected_value,
                    ", ".join(self.summary.data_dates),
                )

        if repository is None or not self.options.writes_database:
            logger.info(
                "Dry run: %s rows ready for %s; nothing saved",
                f"{len(combined):,}",
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
                    logger.info("Kept %s in %s for checking", path.name, destination.parent)
                else:
                    path.unlink(missing_ok=True)
                    logger.debug("Deleted %s now that it is saved", path.name)
            except OSError as exc:
                # Not fatal: housekeeping removes it after FILE_RETENTION_DAYS.
                logger.warning("Could not dispose of %s: %s", path, exc)


def handle_failure(pipeline: AdProductPipeline, error: BaseException) -> None:
    """Hook for failure handling beyond the log.

    The advice for the person reading the log is written in the run's
    result block (see :func:`advice_for`), so there is nothing to add here.
    """
