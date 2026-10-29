"""Configuration, periods, the lock, and the CLI."""

from __future__ import annotations

import threading
from datetime import date, datetime
from pathlib import Path

import pytest

from app.config import load_settings
from app.exceptions import ConfigurationError, LockError
from app.runlock import run_lock
from app.schema import Period
from app.timeutils import UTC, US_PACIFIC_TZ, resolve_report_date
from run import build_parser, resolve_mode


class TestConfig:
    def test_minimal_environment_is_enough(self, env) -> None:
        settings = load_settings()
        assert settings.table == "Advertised_product"
        assert settings.date_format == "%Y-%m-%d"
        assert settings.reports_url_with_entity.endswith("/reports?entityId=ENTITY_TEST")

    @pytest.mark.parametrize("missing", ["DB_HOST", "DB_PASSWORD", "AMAZON_ENTITY_ID"])
    def test_required_values(self, env, monkeypatch, missing: str) -> None:
        monkeypatch.delenv(missing)
        with pytest.raises(ConfigurationError, match=missing):
            load_settings()

    def test_credentials_come_in_pairs(self, env, monkeypatch) -> None:
        monkeypatch.setenv("AMAZON_EMAIL", "a@example.com")
        with pytest.raises(ConfigurationError, match="AMAZON_PASSWORD"):
            load_settings()

    def test_password_is_not_in_repr(self, env) -> None:
        assert "s3cret" not in repr(load_settings())


class TestPeriod:
    def test_presets(self) -> None:
        assert (Period.TODAY.preset, Period.TODAY.offset_days) == ("Today", 0)
        assert (Period.YESTERDAY.preset, Period.YESTERDAY.offset_days) == ("Yesterday", -1)

    def test_yesterday_run_at_14_00_ist_covers_the_previous_us_day(self) -> None:
        # 14:00 IST on 29 Oct = 08:30 UTC = 01:30 PDT on 29 Oct.
        at = datetime(2026, 10, 29, 8, 30, tzinfo=UTC)
        assert resolve_report_date(US_PACIFIC_TZ, offset_days=-1, reference=at) == date(2026, 10, 28)
        assert resolve_report_date(US_PACIFIC_TZ, reference=at) == date(2026, 10, 29)


class TestLock:
    def test_second_run_waits_then_gives_up(self, tmp_path: Path) -> None:
        lock = tmp_path / "x.lock"
        held = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with run_lock(lock):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        try:
            with pytest.raises(LockError, match="after waiting"):
                with run_lock(lock, wait_seconds=0.01, sleep=lambda _s: None):
                    pass
        finally:
            release.set()
            thread.join()

    def test_acquires_once_free(self, tmp_path: Path) -> None:
        with run_lock(tmp_path / "x.lock", wait_seconds=1):
            pass


class TestCli:
    def test_default_is_a_today_run(self) -> None:
        args = build_parser().parse_args([])
        assert args.period == "today"
        assert resolve_mode(args).value == "full"

    def test_yesterday(self) -> None:
        assert build_parser().parse_args(["--period", "yesterday"]).period == "yesterday"

    def test_modes_are_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--login", "--download-only"])
