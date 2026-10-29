"""Selector resolution: the candidate search and its timeout budget.

The budget is the point of these tests. ``find_first`` used to apply its
timeout to *each* candidate, so a five-entry list with a 60 s timeout could
block for five minutes with no output -- indistinguishable from a hang.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.exceptions import ConfigurationError
from app.selectors import candidates, find_first, is_present, load_selectors


class FakeLocator:
    """Stands in for a Playwright locator with a scripted outcome."""

    def __init__(self, selector: str, matches: bool, calls: list[tuple[str, int]]) -> None:
        self.selector = selector
        self._matches = matches
        self._calls = calls

    @property
    def first(self) -> FakeLocator:
        return self

    def wait_for(self, *, state: str, timeout: int) -> None:
        self._calls.append((self.selector, timeout))
        if not self._matches:
            raise PlaywrightTimeoutError(f"no match for {self.selector}")


class FakePage:
    """Minimal page whose locators match only the selectors listed."""

    def __init__(self, matching: set[str] | None = None) -> None:
        self.matching = matching or set()
        self.calls: list[tuple[str, int]] = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(selector, selector in self.matching, self.calls)


@pytest.fixture
def catalogue(tmp_path: Path) -> Path:
    path = tmp_path / "selectors.json"
    path.write_text(
        json.dumps({"_comment": "ignored", "thing": ["#first", "#second", "#third"]}),
        encoding="utf-8",
    )
    load_selectors.cache_clear()
    yield path
    load_selectors.cache_clear()


class TestCatalogue:
    def test_loads_candidate_lists(self, catalogue: Path) -> None:
        assert candidates("thing", catalogue) == ("#first", "#second", "#third")

    def test_comment_keys_are_ignored(self, catalogue: Path) -> None:
        assert "_comment" not in load_selectors(catalogue)

    def test_unknown_key_is_rejected(self, catalogue: Path) -> None:
        with pytest.raises(ConfigurationError, match="No selector candidates"):
            candidates("nope", catalogue)

    def test_missing_file_is_rejected(self, tmp_path: Path) -> None:
        load_selectors.cache_clear()
        with pytest.raises(ConfigurationError, match="not found"):
            load_selectors(tmp_path / "absent.json")

    def test_malformed_file_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        load_selectors.cache_clear()

        with pytest.raises(ConfigurationError, match="not valid JSON"):
            load_selectors(path)

    def test_non_list_entry_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"thing": "#one"}), encoding="utf-8")
        load_selectors.cache_clear()

        with pytest.raises(ConfigurationError, match="list of selector strings"):
            load_selectors(path)


class TestSearch:
    def test_returns_the_first_matching_candidate(self, catalogue: Path) -> None:
        page = FakePage({"#second"})

        found = find_first(page, "thing", timeout_ms=5_000, path=catalogue)

        assert found is not None
        assert found.selector == "#second"

    def test_stops_probing_once_matched(self, catalogue: Path) -> None:
        page = FakePage({"#first"})

        find_first(page, "thing", timeout_ms=5_000, path=catalogue)

        assert [selector for selector, _ in page.calls] == ["#first"]

    def test_returns_none_when_nothing_matches(self, catalogue: Path) -> None:
        assert find_first(FakePage(), "thing", timeout_ms=300, path=catalogue) is None

    def test_timeout_is_a_budget_for_the_whole_search_not_per_candidate(
        self, catalogue: Path
    ) -> None:
        """The regression that made --login look like it had hung.

        Three candidates and a long budget must not mean three long waits: no
        single probe may exceed the short per-candidate cap.
        """
        page = FakePage()

        find_first(page, "thing", timeout_ms=1_000, path=catalogue)

        assert page.calls, "expected at least one probe"
        assert max(timeout for _, timeout in page.calls) <= 2_500

    def test_the_whole_search_stays_within_its_budget(self, catalogue: Path) -> None:
        import time

        page = FakePage()
        started = time.monotonic()

        find_first(page, "thing", timeout_ms=500, path=catalogue)

        # FakeLocator returns instantly, so this asserts the loop exits on the
        # deadline rather than cycling forever.
        assert time.monotonic() - started < 5

    def test_instant_failures_do_not_spin_the_cpu(self, catalogue: Path) -> None:
        """Probes that return immediately must not burn the budget in a hot loop."""
        page = FakePage()

        find_first(page, "thing", timeout_ms=1_000, path=catalogue)

        # Three candidates, ~0.1 s between passes over a 1 s budget: tens of
        # probes, not tens of thousands.
        assert len(page.calls) < 200

    def test_every_candidate_is_probed_before_giving_up(self, catalogue: Path) -> None:
        page = FakePage()

        find_first(page, "thing", timeout_ms=400, path=catalogue)

        assert {selector for selector, _ in page.calls} == {"#first", "#second", "#third"}

    def test_a_broken_selector_does_not_abort_the_search(self, catalogue: Path) -> None:
        class ExplodingPage(FakePage):
            def locator(self, selector: str) -> FakeLocator:
                if selector == "#first":
                    raise ValueError("malformed selector")
                return super().locator(selector)

        page = ExplodingPage({"#third"})

        found = find_first(page, "thing", timeout_ms=5_000, path=catalogue)

        assert found is not None
        assert found.selector == "#third"


class TestPresence:
    def test_true_when_a_candidate_matches(self, catalogue: Path) -> None:
        assert is_present(FakePage({"#third"}), "thing", timeout_ms=500, path=catalogue) is True

    def test_false_when_none_match(self, catalogue: Path) -> None:
        assert is_present(FakePage(), "thing", timeout_ms=200, path=catalogue) is False


class TestPlaceholders:
    def test_placeholders_are_filled_from_params(self, tmp_path: Path) -> None:
        path = tmp_path / "selectors.json"
        path.write_text(
            json.dumps({"option": ["li:text-is('{value}')", "role=option[name=/^{value}$/i]"]}),
            encoding="utf-8",
        )
        load_selectors.cache_clear()
        page = FakePage({"role=option[name=/^Daily$/i]"})

        found = find_first(page, "option", timeout_ms=5_000, path=path, params={"value": "Daily"})

        assert found is not None
        assert [selector for selector, _ in page.calls] == [
            "li:text-is('Daily')",
            "role=option[name=/^Daily$/i]",
        ]
        load_selectors.cache_clear()

    def test_other_braces_survive(self, tmp_path: Path) -> None:
        path = tmp_path / "selectors.json"
        path.write_text(json.dumps({"x": [r"text=/\d{2}/"]}), encoding="utf-8")
        load_selectors.cache_clear()
        page = FakePage({r"text=/\d{2}/"})

        assert find_first(page, "x", timeout_ms=1_000, path=path, params={"value": "v"})
        load_selectors.cache_clear()


class TestShippedCatalogue:
    """Every key the code asks for must exist in config/selectors.json."""

    USED_KEYS = (
        "signed_in_marker", "sign_in_marker", "security_challenge_marker",
        "signin_email_input", "signin_continue_button", "signin_password_input",
        "signin_submit_button", "signin_remember_me", "signin_error_message",
        "otp_input", "otp_remember_device", "otp_submit_button", "captcha_marker",
        "loading_indicator", "create_report_button", "search_reports_input",
        "report_row", "row_download_button", "new_report_marker", "form_dropdown",
        "dropdown_option", "time_unit_option", "report_period_button",
        "date_preset_option", "date_save_button", "report_name_input",
        "run_report_button",
    )

    def test_every_used_key_is_configured(self) -> None:
        load_selectors.cache_clear()
        catalogue = load_selectors()
        assert [key for key in self.USED_KEYS if key not in catalogue] == []
