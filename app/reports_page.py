"""Amazon Ads "Sponsored ads reports" page automation.

Drives the public web UI exactly as the manual process in the screenshots
does: open the reports list for the configured entity, click **Create report**,
pick the category and report type, choose **Daily**, choose the **Today** or
**Yesterday** period, and click **Run report**.  Amazon then builds the report
in the background; :mod:`app.downloader` waits for it and downloads it.

Every created report is given a unique name (``REPORT_NAME_PREFIX`` + report
+ period + UTC timestamp) so the run can find *its* report in a list that
already holds dozens with Amazon's default names.

Security boundaries this module will not cross:
  * It never solves a CAPTCHA or answers an OTP/MFA prompt.  Those states
    raise :class:`AuthenticationError` and stop the run.
  * It never suppresses or spoofs bot detection.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from time import monotonic

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .auth import sign_in
from .browser import BrowserSession, safe_url
from .config import Settings
from .exceptions import AuthenticationError, NavigationError, ReportConfigError
from .logging_config import get_logger
from .schema import Period, ReportSpec
from .selectors import candidates, find_first, is_present

logger = get_logger("reports")

#: How long an opened dropdown or date picker gets to show its options.
OPTION_WAIT_MS = 15_000
#: Pause after a choice so the form can re-render (a new category resets the
#: report type, and the form below it is redrawn).
SETTLE_MS = 750

_SPACES = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _SPACES.sub(" ", text).strip().casefold()


def _text_of(locator: Locator) -> str:
    try:
        return _SPACES.sub(" ", locator.inner_text(timeout=3_000)).strip()
    except Exception:
        return ""


class ReportsPage:
    """High-level operations against the Sponsored ads reports UI."""

    def __init__(self, session: BrowserSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    @property
    def page(self) -> Page:
        return self.session.page

    # -- navigation --------------------------------------------------------

    def open(self) -> None:
        """Navigate to the reports list for the configured entity.

        The persistent profile normally means we land straight on the list.  If
        the session has lapsed and credentials are configured, sign in once and
        come back; anything beyond that -- a CAPTCHA, an OTP prompt -- ends the
        run and asks for a person.
        """
        self.goto_list()

        if self.is_showing_sign_in():
            if not self.settings.can_auto_login:
                self.session.capture_failure("sign-in-required")
                raise AuthenticationError(
                    "Amazon session is missing or expired (sign-in page shown), and "
                    "automatic sign-in is not configured. Set AMAZON_EMAIL and "
                    "AMAZON_PASSWORD, or re-authenticate with: python run.py --login"
                )
            sign_in(self.session, self.settings)
            # Amazon usually redirects back, but not always to the right entity.
            if not is_present(self.page, "signed_in_marker", timeout_ms=5_000):
                self.goto_list()

        self.assert_authenticated()
        self.wait_for_list()
        logger.info("Reports page loaded: %s", safe_url(self.page.url))

    def goto_list(self) -> None:
        logger.info("Navigating to Sponsored ads reports (entity %s)", self.settings.entity_id)
        try:
            self.page.goto(self.settings.reports_url_with_entity, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            raise NavigationError(f"Timed out loading the reports page: {exc}") from exc
        except Exception as exc:
            raise NavigationError(f"Could not load the reports page: {exc}") from exc

    def is_showing_sign_in(self) -> bool:
        """Whether Amazon is currently asking us to sign in."""
        url = self.page.url.lower()
        if any(marker in url for marker in ("/ap/signin", "/ap/mfa", "signin")):
            return True
        return is_present(self.page, "sign_in_marker", timeout_ms=1_500)

    def assert_authenticated(self) -> None:
        """Stop the run if Amazon is showing sign-in or a security challenge."""
        if is_present(self.page, "security_challenge_marker", timeout_ms=1_500):
            self.session.capture_failure("security-challenge")
            raise AuthenticationError(
                "Amazon presented a security challenge (CAPTCHA/OTP/MFA). "
                "AdProduct does not answer these. Complete it once by hand with "
                "'python run.py --login'; the session is then reused."
            )
        if self.is_showing_sign_in():
            self.session.capture_failure("sign-in-required")
            raise AuthenticationError(
                "Amazon is still showing the sign-in page. If credentials are set, "
                "they may be wrong or the account needs a challenge answered by a "
                "person: python run.py --login"
            )

    def wait_for_list(self) -> None:
        """Block until the reports list (its Create report button) is rendered."""
        marker = find_first(
            self.page, "signed_in_marker", timeout_ms=self.settings.browser_timeout_ms
        )
        if marker is None:
            self.session.capture_failure("reports-list-missing")
            raise NavigationError(
                "The reports list never appeared within "
                f"{self.settings.browser_timeout_ms} ms at {safe_url(self.page.url)}. "
                "Either the page had not finished loading, or the layout changed -- "
                "add the current selector to config/selectors.json ('signed_in_marker')."
            )
        self.wait_for_idle()

    def wait_for_idle(self) -> None:
        """Wait (bounded) for spinners to clear and the network to settle."""
        for selector in candidates("loading_indicator"):
            try:
                self.page.locator(selector).first.wait_for(state="hidden", timeout=10_000)
            except Exception:
                continue
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            logger.debug("networkidle not reached; continuing")

    # -- creating a report -------------------------------------------------

    def create_report(self, spec: ReportSpec, period: Period, name: str) -> str | None:
        """Fill in the "New report" form and run it (screenshots 2-8).

        Returns the URL of the report's own History page, which is where
        Amazon lands after "Run report" (verified 2026-10-29: the page shows
        "Report: <name>", a Status column that starts at "Pending", and an
        Action column).  None if Amazon went back to the list instead.

        Raises:
            ReportConfigError: A control is missing or did not take the value.
        """
        logger.info("Creating %s report for %s as %r", spec.label, period.preset, name)
        self._click("create_report_button", "Create report")
        if find_first(self.page, "new_report_marker", timeout_ms=self.settings.browser_timeout_ms) is None:
            self._fail("new-report-form-missing", "The 'New report' form did not open")
        self.wait_for_idle()

        self._choose("Report category", spec.category)
        self._choose("Report type", spec.report_type)
        self._choose_time_unit("Daily")
        self._choose_period(period.preset)
        self._set_report_name(name)

        self._click("run_report_button", "Run report")
        # Amazon opens the new report's History page. If it stays on the form
        # instead, the form is showing a validation error -- the screenshot
        # says which.
        try:
            self.page.wait_for_url(
                re.compile(r"/reports/history/"), timeout=self.settings.browser_timeout_ms
            )
        except PlaywrightTimeoutError:
            if find_first(self.page, "signed_in_marker", timeout_ms=2_000) is None:
                self._fail(
                    f"run-report-{spec.key}",
                    f"After 'Run report' for {spec.label}, Amazon showed neither the "
                    "report's History page nor the report list",
                )
            logger.info("Report requested: %s (back on the list)", name)
            return None
        self.wait_for_idle()
        history_url = self.page.url
        logger.info("Report requested: %s (history page %s)", name, safe_url(history_url))
        return history_url

    def _choose(self, label: str, value: str) -> None:
        """Set a dropdown on the form (Report category / Report type)."""
        params = {"label": label}
        trigger = find_first(
            self.page, "form_dropdown", params=params, timeout_ms=self.settings.browser_timeout_ms
        )
        if trigger is None:
            self._fail(
                f"{label}-missing",
                f"Could not find the {label!r} dropdown (config/selectors.json 'form_dropdown')",
            )
        if _norm(_text_of(trigger)) == _norm(value):
            logger.info("%s already set: %s", label, value)
            return

        trigger.click()
        option = find_first(
            self.page, "dropdown_option", params={"value": value}, timeout_ms=OPTION_WAIT_MS
        )
        if option is None:
            self.page.keyboard.press("Escape")
            self._fail(
                f"{label}-option-missing",
                f"{label} did not offer {value!r} (config/selectors.json 'dropdown_option')",
            )
        option.click()
        self.page.wait_for_timeout(SETTLE_MS)

        shown = self._wait_for_text(
            "form_dropdown", params, lambda text: _norm(text) == _norm(value)
        )
        if _norm(shown) != _norm(value):
            self._fail(f"{label}-not-set", f"{label} shows {shown!r} after choosing {value!r}")
        logger.info("%s set: %s", label, value)

    def _wait_for_text(
        self, key: str, params: dict[str, str] | None, accept: Callable[[str], bool]
    ) -> str:
        """Re-read a control until its text passes ``accept``; return the last text.

        After a choice the form re-renders, slowly on a small server: a single
        read right away saw an empty control on 2026-10-30 although the value
        had been taken.
        """
        deadline = monotonic() + self.settings.browser_timeout_ms / 1000
        shown = ""
        while True:
            control = find_first(self.page, key, params=params, timeout_ms=5_000)
            shown = _text_of(control) if control is not None else ""
            if accept(shown) or monotonic() >= deadline:
                return shown
            self.page.wait_for_timeout(500)

    def _choose_time_unit(self, value: str) -> None:
        """Tick the Time unit radio (screenshots 5, 13, 22)."""
        if self._radio_checked(value):
            logger.info("Time unit already set: %s", value)
            return
        radio = find_first(
            self.page,
            "time_unit_option",
            params={"value": value},
            timeout_ms=self.settings.browser_timeout_ms,
        )
        if radio is None:
            self._fail(
                "time-unit-missing",
                f"Could not find the {value!r} time unit (config/selectors.json 'time_unit_option')",
            )
        radio.click()
        self.page.wait_for_timeout(SETTLE_MS)
        if self._radio_checked(value) is False:
            self._fail("time-unit-not-set", f"Time unit {value!r} did not become selected")
        logger.info("Time unit set: %s", value)

    def _radio_checked(self, value: str) -> bool | None:
        """Whether the named radio is checked; None when it cannot be read."""
        try:
            return self.page.get_by_role("radio", name=value, exact=True).first.is_checked(
                timeout=2_000
            )
        except Exception:
            return None

    def _choose_period(self, preset: str) -> None:
        """Pick a Report period preset (screenshots 6-7)."""
        trigger = find_first(
            self.page, "report_period_button", timeout_ms=self.settings.browser_timeout_ms
        )
        if trigger is None:
            self._fail(
                "report-period-missing",
                "Could not find the 'Report period' button "
                "(config/selectors.json 'report_period_button')",
            )
        if _norm(_text_of(trigger)) == _norm(preset):
            logger.info("Report period already set: %s", preset)
            return

        trigger.click()
        option = find_first(
            self.page, "date_preset_option", params={"value": preset}, timeout_ms=OPTION_WAIT_MS
        )
        if option is None:
            self.page.keyboard.press("Escape")
            self._fail(
                "report-period-option-missing",
                f"The date picker did not offer {preset!r} "
                "(config/selectors.json 'date_preset_option')",
            )
        option.click()
        # Some layouts apply a preset at once, others need Save.
        save = find_first(self.page, "date_save_button", timeout_ms=3_000)
        if save is not None:
            save.click()
        self.page.wait_for_timeout(SETTLE_MS)

        shown = self._wait_for_text(
            "report_period_button", None, lambda text: _norm(preset) in _norm(text)
        )
        if _norm(preset) not in _norm(shown):
            self._fail(
                "report-period-not-set", f"Report period shows {shown!r} after choosing {preset!r}"
            )
        logger.info("Report period set: %s", preset)

    def _set_report_name(self, name: str) -> None:
        field = find_first(
            self.page, "report_name_input", timeout_ms=self.settings.browser_timeout_ms
        )
        if field is None:
            self._fail(
                "report-name-missing",
                "Could not find the 'Report name' box (config/selectors.json "
                "'report_name_input'). The unique name is how the run finds its report.",
            )
        field.fill(name)
        if field.input_value(timeout=3_000) != name:
            self._fail("report-name-not-set", "The 'Report name' box did not keep the name")

    # -- finding a report --------------------------------------------------

    def find_report_row(self, name: str) -> Locator | None:
        """The list row for the report called ``name``, if it is listed."""
        search = find_first(self.page, "search_reports_input", timeout_ms=3_000)
        if search is not None:
            search.fill(name)
            search.press("Enter")
            self.wait_for_idle()
        return find_first(self.page, "report_row", params={"name": name}, timeout_ms=10_000)

    def open_history(self, url: str) -> Locator | None:
        """Reload a report's History page and return its latest run row."""
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            raise NavigationError(f"Could not load the report history page: {exc}") from exc
        self.wait_for_idle()
        return find_first(self.page, "history_row", timeout_ms=15_000)

    def download_button(self, row: Locator) -> Locator | None:
        """The row's download icon -- present only once the report is ready."""
        return find_first(row, "row_download_button", timeout_ms=2_000)

    # -- helpers -----------------------------------------------------------

    def _click(self, key: str, what: str) -> None:
        control = find_first(self.page, key, timeout_ms=self.settings.browser_timeout_ms)
        if control is None:
            self._fail(f"{key}-missing", f"Could not find {what!r} (config/selectors.json {key!r})")
        control.click()

    def _fail(self, label: str, message: str):  # -> NoReturn
        self.session.capture_failure(label.lower().replace(" ", "-"))
        raise ReportConfigError(message + ". A screenshot is in the screenshot directory.")
