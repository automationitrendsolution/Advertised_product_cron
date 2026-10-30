"""Playwright browser lifecycle.

Uses a *persistent* Chromium context so that a human-completed sign-in --
including whatever CAPTCHA, OTP or MFA Amazon asks for -- is stored once and
reused on later headless runs.  AdProduct never automates those challenges; it
only reuses a session a person established.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import TracebackType

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

from urllib.parse import urlsplit

from .config import Settings
from .exceptions import NavigationError
from .logging_config import get_logger
from .timeutils import now_utc

logger = get_logger("browser")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_url(url: str) -> str:
    """Scheme, host and path only.

    Amazon's sign-in redirect carries an ``ssoResponse`` token in the query
    string; logging the raw URL would write a live credential to disk.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.scheme and not parts.netloc:
        return url.split("?", 1)[0]
    trimmed = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f"{trimmed}?<query redacted>" if parts.query else trimmed


class BrowserSession:
    """Owns the Playwright driver, the persistent context and the page.

    Always use it as a context manager -- ``__exit__`` closes the context and
    stops the driver even when the body raised, so a failed run never leaks a
    Chromium process on the server.
    """

    def __init__(self, settings: Settings, *, headless: bool | None = None) -> None:
        self.settings = settings
        self.headless = settings.headless if headless is None else headless
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> BrowserSession:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is not None:
            self.capture_failure("unhandled-error")
        self.close()

    def start(self) -> None:
        self.settings.session_dir.mkdir(parents=True, exist_ok=True)
        self.settings.download_dir.mkdir(parents=True, exist_ok=True)
        # The report form's date picker is drawn from the browser's clock.
        # Amazon says the presets follow the campaign's country, but running
        # Chromium in that same zone means the calendar it shows agrees too,
        # instead of being a day ahead in IST for part of every day.
        timezone_id = self.settings.report_timezone.key
        logger.debug(
            "Launching Chromium (headless=%s, timezone=%s, session_dir=%s)",
            self.headless,
            timezone_id,
            self.settings.session_dir,
        )
        # Headless: a fixed page size, so every run renders the same layout.
        # Headed: a maximized window whose page follows the window -- a fixed
        # 1920x1080 page in a smaller laptop window is cut off on screen.
        if self.headless:
            sizing: dict[str, object] = {
                "viewport": {
                    "width": self.settings.viewport_width,
                    "height": self.settings.viewport_height,
                }
            }
            args = list(self.settings.browser_args)
        else:
            sizing = {"no_viewport": True}
            args = [*self.settings.browser_args, "--start-maximized"]

        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.session_dir),
                headless=self.headless,
                accept_downloads=True,
                downloads_path=str(self.settings.download_dir),
                args=args,
                slow_mo=self.settings.slow_mo_ms or 0,
                user_agent=self.settings.user_agent,
                locale="en-US",
                timezone_id=timezone_id,
                **sizing,
            )
        except Exception as exc:  # pragma: no cover - launch failures are env-specific
            self.close()
            raise NavigationError(f"Failed to launch Chromium: {exc}") from exc

        self._context.set_default_timeout(self.settings.browser_timeout_ms)
        self._context.set_default_navigation_timeout(self.settings.browser_timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        logger.info("Browser started (%s)", "hidden" if self.headless else "visible window")

    def close(self) -> None:
        """Close the context and stop the driver.  Never raises."""
        if self._context is not None:
            try:
                self._context.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("Error closing browser context: %s", exc)
            finally:
                self._context = None
                self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("Error stopping Playwright: %s", exc)
            finally:
                self._playwright = None
        logger.debug("Browser resources released")

    # -- accessors ---------------------------------------------------------

    @property
    def page(self) -> Page:
        if self._page is None:
            raise NavigationError("Browser session is not started")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise NavigationError("Browser session is not started")
        return self._context

    @property
    def is_started(self) -> bool:
        return self._context is not None

    # -- diagnostics -------------------------------------------------------

    def screenshot(self, label: str) -> Path | None:
        """Save a full-page screenshot.  Returns the path, or None on failure."""
        if self._page is None:
            return None
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        safe = _SAFE_NAME.sub("-", label).strip("-") or "screenshot"
        path = self.settings.screenshot_dir / f"{stamp}_{safe}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(path=str(path), full_page=True)
            logger.debug("Saved screenshot %s", path)
            return path
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning("Could not capture screenshot %s: %s", label, exc)
            return None

    def capture_failure(self, label: str) -> dict[str, str]:
        """Collect a screenshot, the current URL and the page title.

        The URL is stripped of its query string and page *content* is never
        dumped: Amazon carries SSO responses and session tokens in both.
        """
        details: dict[str, str] = {}
        if self._page is None:
            return details
        try:
            details["url"] = safe_url(self._page.url)
            details["title"] = self._page.title()
        except Exception as exc:  # pragma: no cover - page may be dead
            logger.debug("Could not read page metadata: %s", exc)
        shot = self.screenshot(label)
        if shot is not None:
            details["screenshot"] = str(shot)
        if details:
            logger.error(
                "Picture of the page saved to %s (page title: %r, address: %s)",
                details.get("screenshot", "<not saved>"),
                details.get("title", "?"),
                details.get("url", "?"),
            )
        return details
