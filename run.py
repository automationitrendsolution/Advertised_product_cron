#!/usr/bin/env python3
"""AdProduct command-line entry point.

Builds the three advertised-product reports on the Amazon Ads "Sponsored ads
reports" page with Playwright, downloads them, and loads them into MySQL.

Modes
-----
    python run.py --period today              # production run (every 3 hours)
    python run.py --period yesterday          # production run (daily, 14:00 IST)
    python run.py --period today --headed     # visible browser, for debugging
    python run.py --period today --dry-run    # full workflow, no MySQL writes
    python run.py --period today --download-only
    python run.py --process-files a.xlsx b.csv c.xlsx   # load hand-downloaded files
    python run.py --login                     # one-time Amazon session setup

Running under cron or systemd
-----------------------------
cron starts in ``$HOME`` with a near-empty environment.  AdProduct reads
``.env`` from *the project directory*, so the job must ``cd`` there first and
call ``uv`` by absolute path -- see ``deploy/adproduct.cron``.

Runs never overlap: each takes an exclusive ``flock`` on ``LOCK_FILE``.  A run
that finds the lock held *waits* for it (``LOCK_WAIT_SECONDS``) instead of
being skipped, so the once-a-day "yesterday" run is never lost to a "today"
run that happens to be in progress.

``--login`` is a one-time manual step that cron cannot do: Amazon may ask for
a two-step code, and only a person can supply it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import __version__
from app.config import load_settings
from app.exceptions import AdProductError, ConfigurationError
from app.logging_config import configure_logging, get_logger
from app.main import AdProductPipeline, RunMode, RunOptions, handle_failure
from app.schema import Period

_HELP_EPILOG = """
examples:
  python run.py --period today              every 3 hours (what cron calls)
  python run.py --period yesterday          once a day at 14:00 IST
  python run.py --period today --headed     watch it in a visible browser
  python run.py --period today --dry-run    full workflow, nothing written to MySQL
  python run.py --process-files SP.xlsx SB.csv SD.xlsx
                                            load reports downloaded by hand
  python run.py --login                     one-time sign-in

exit codes:
  0 success · 2 config · 3 authentication (needs a human) · 4 navigation
  5 report form · 6 download · 7 report file · 9 database
  10 all reports empty (nothing written) · 11 lock wait timed out
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adproduct",
        description=(
            "Build the Sponsored Products / Brands / Display advertised-product reports "
            "on Amazon Ads and load them into MySQL."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"AdProduct {__version__}")
    parser.add_argument(
        "--period",
        choices=[period.value for period in Period],
        default=Period.TODAY.value,
        help="Report period preset to request (default: today).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and process, but write nothing to MySQL.",
    )

    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--download-only",
        action="store_true",
        help="Download the three reports and stop (no processing, no database).",
    )
    modes.add_argument(
        "--process-files",
        nargs="+",
        type=Path,
        metavar="PATH",
        help="Load the three report files given (CSV/XLSX, any order) without opening Amazon.",
    )
    modes.add_argument(
        "--login",
        action="store_true",
        help=(
            "One-time Amazon sign-in by a person. Opens a visible browser when there "
            "is a display; otherwise asks for the two-step code in this terminal."
        ),
    )

    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window (overrides HEADLESS). Local debugging only.",
    )
    parser.add_argument("--env-file", type=Path, metavar="PATH", help="Alternative .env file.")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override LOG_LEVEL for this run.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="No console output; log to LOG_DIR/adproduct.log only (for cron).",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Skip the single-instance lock. Only for manual, supervised runs.",
    )
    parser.add_argument(
        "--echo-sql",
        action="store_true",
        help="Log SQL statements (verbose; never logs credentials).",
    )
    return parser


def resolve_mode(args: argparse.Namespace) -> RunMode:
    if args.login:
        return RunMode.LOGIN
    if args.process_files:
        return RunMode.PROCESS_FILES
    if args.download_only:
        return RunMode.DOWNLOAD_ONLY
    return RunMode.FULL


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = resolve_mode(args)

    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return ConfigurationError.exit_code

    settings.ensure_directories()
    configure_logging(
        settings.log_dir,
        level=args.log_level or settings.log_level,
        retention_days=settings.log_retention_days,
        console=not args.quiet,
    )
    logger = get_logger("cli")

    for path in args.process_files or []:
        if not path.expanduser().is_file():
            logger.error("File not found: %s", path)
            return ConfigurationError.exit_code

    options = RunOptions(
        mode=mode,
        period=Period(args.period),
        dry_run=args.dry_run,
        headed=True if args.headed else None,
        files=list(args.process_files or []),
        use_lock=not args.no_lock,
        echo_sql=args.echo_sql,
    )

    pipeline = AdProductPipeline(settings, options)
    try:
        pipeline.run()
    except AdProductError as exc:
        handle_failure(pipeline, exc)
        return exc.exit_code
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception:  # noqa: BLE001 - already logged with traceback
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
