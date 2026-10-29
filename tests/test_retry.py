"""Retry policy: transient failures only, with exponential backoff."""

from __future__ import annotations

import pytest
from playwright.sync_api import Error as PlaywrightError

from app.exceptions import (
    AuthenticationError,
    ConfigurationError,
    DownloadError,
    ReportConfigError,
    NavigationError,
)
from app.retry import compute_delay, is_transient, retry_call


def never_sleep(_seconds: float) -> None:
    """Keep the tests fast; backoff timing is asserted separately."""


class TestTransientClassification:
    @pytest.mark.parametrize(
        "error",
        [
            NavigationError("x"),
            DownloadError("x"),
            ReportConfigError("x"),
            TimeoutError(),
            PlaywrightError("Target page, context or browser has been closed"),
        ],
    )
    def test_transient_errors(self, error: BaseException) -> None:
        assert is_transient(error) is True

    @pytest.mark.parametrize(
        "error", [AuthenticationError("x"), ConfigurationError("x"), ValueError("x")]
    )
    def test_non_transient_errors(self, error: BaseException) -> None:
        assert is_transient(error) is False


class TestRetryCall:
    def test_returns_on_first_success(self) -> None:
        calls: list[int] = []

        def operation() -> str:
            calls.append(1)
            return "done"

        assert retry_call(operation, description="op", attempts=3, base_delay=0, max_delay=0,
                          sleep=never_sleep) == "done"
        assert len(calls) == 1

    def test_retries_transient_failures_then_succeeds(self) -> None:
        attempts: list[int] = []

        def operation() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise NavigationError("temporary")
            return "recovered"

        result = retry_call(operation, description="op", attempts=3, base_delay=0, max_delay=0,
                            sleep=never_sleep)

        assert result == "recovered"
        assert len(attempts) == 3

    def test_gives_up_after_the_configured_attempts(self) -> None:
        attempts: list[int] = []

        def operation() -> None:
            attempts.append(1)
            raise DownloadError("still failing")

        with pytest.raises(DownloadError):
            retry_call(operation, description="op", attempts=4, base_delay=0, max_delay=0,
                       sleep=never_sleep)

        assert len(attempts) == 4

    def test_authentication_failure_is_never_retried(self) -> None:
        attempts: list[int] = []

        def operation() -> None:
            attempts.append(1)
            raise AuthenticationError("CAPTCHA presented")

        with pytest.raises(AuthenticationError):
            retry_call(operation, description="op", attempts=5, base_delay=0, max_delay=0,
                       sleep=never_sleep)

        assert len(attempts) == 1

    def test_sleeps_between_attempts(self) -> None:
        slept: list[float] = []

        def operation() -> None:
            raise NavigationError("nope")

        with pytest.raises(NavigationError):
            retry_call(operation, description="op", attempts=3, base_delay=4, max_delay=60,
                       sleep=slept.append)

        assert len(slept) == 2  # no sleep after the final failure

    def test_on_retry_hook_is_called_per_retry(self) -> None:
        seen: list[int] = []

        def operation() -> None:
            raise NavigationError("nope")

        with pytest.raises(NavigationError):
            retry_call(operation, description="op", attempts=3, base_delay=0, max_delay=0,
                       on_retry=lambda attempt, _exc: seen.append(attempt), sleep=never_sleep)

        assert seen == [1, 2]


class TestBackoff:
    def test_delay_grows_and_stays_within_the_cap(self) -> None:
        for attempt in range(1, 8):
            assert 0 <= compute_delay(attempt, base=5, maximum=60) <= 60

    def test_zero_base_disables_waiting(self) -> None:
        assert compute_delay(3, base=0, maximum=60) == 0.0

    def test_growth_is_exponential_in_the_upper_bound(self) -> None:
        # Full jitter samples [0, exp); assert the bound, not a sample.
        samples = [compute_delay(1, base=5, maximum=1000) for _ in range(200)]
        later = [compute_delay(4, base=5, maximum=1000) for _ in range(200)]

        assert max(samples) <= 5
        assert max(later) <= 40
