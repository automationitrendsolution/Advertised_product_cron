"""Environment-driven configuration.

Nothing here reads a credential from disk or from source; every secret arrives
through the process environment (populated from ``.env`` locally, or from a
systemd ``EnvironmentFile`` in production).  Validation is strict and fails
fast: a run that starts with a bad DSN should never get as far as opening a
browser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Timezones the pipeline is defined in terms of.  The server's own local time
#: is deliberately never consulted.
UTC = ZoneInfo("UTC")
INDIA_TZ = ZoneInfo("Asia/Kolkata")
US_PACIFIC_TZ = ZoneInfo("US/Pacific")

DEFAULT_REPORTS_URL = "https://advertising.amazon.com/reports"
DEFAULT_TABLE = "Advertised_product"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _require(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is not set")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigurationError(
        f"{name} must be a boolean (true/false), got {raw!r}"
    )


def _get_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _get_path(name: str, default: Path) -> Path:
    raw = _get(name)
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


#: Flags that make headless Chromium behave on a small server.  A default
#: 64 MB /dev/shm is exhausted by Chromium, which then crashes.
_DEFAULT_BROWSER_ARGS: tuple[str, ...] = ("--disable-dev-shm-usage",)


def _browser_args() -> tuple[str, ...]:
    """Chromium flags: the server-safe defaults plus anything in ``BROWSER_ARGS``."""
    args = list(_DEFAULT_BROWSER_ARGS)
    extra = _get("BROWSER_ARGS")
    if extra:
        args.extend(part.strip() for part in extra.split(",") if part.strip())
    if _get_bool("BROWSER_NO_SANDBOX", False):
        args.append("--no-sandbox")
    return tuple(dict.fromkeys(args))


def _get_zone(name: str, default: str) -> ZoneInfo:
    raw = _get(name, default) or default
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            f"{name}={raw!r} is not a valid IANA timezone. "
            "Install the 'tzdata' package if the system zone database is missing."
        ) from exc


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    pool_recycle_seconds: int = 1800
    connect_timeout_seconds: int = 30
    #: Path to a CA bundle when the server requires TLS (e.g. isrgrootx1.pem).
    ssl_ca: Path | None = None
    #: Verify the server certificate against the CA.  Leave off only when the
    #: server presents a certificate that cannot be validated.
    ssl_verify_cert: bool = False
    #: Disable TLS entirely.  Only for a server on a trusted private network.
    ssl_disabled: bool = False

    def connect_args(self) -> dict[str, object]:
        """Driver kwargs for mysql-connector-python."""
        args: dict[str, object] = {"connection_timeout": self.connect_timeout_seconds}
        if self.ssl_disabled:
            args["ssl_disabled"] = True
            return args
        if self.ssl_ca is not None:
            args["ssl_ca"] = str(self.ssl_ca)
            args["ssl_verify_cert"] = self.ssl_verify_cert
        return args

    @property
    def url(self) -> str:
        """SQLAlchemy URL.  Credentials are percent-encoded, never logged."""
        return (
            f"mysql+mysqlconnector://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @property
    def safe_url(self) -> str:
        """Same URL with the password redacted -- this is the loggable form."""
        return (
            f"mysql+mysqlconnector://{self.user}:***@{self.host}:{self.port}/{self.name}"
        )

    def __str__(self) -> str:  # pragma: no cover - defensive
        return self.safe_url


@dataclass(frozen=True)
class Settings:
    """Fully validated runtime configuration."""

    reports_url: str
    entity_id: str
    database: DatabaseConfig

    #: Amazon sign-in credentials.  Optional: when absent, AdProduct relies
    #: purely on the persistent session and asks for a human once it expires.
    amazon_email: str | None
    amazon_password: str | None = field(repr=False)
    #: Whether to fill the sign-in form automatically when the session has
    #: lapsed.  Has no effect unless both credentials are set.
    auto_login: bool

    download_dir: Path
    processed_dir: Path
    failed_dir: Path
    log_dir: Path
    screenshot_dir: Path
    session_dir: Path
    lock_file: Path

    headless: bool
    browser_timeout_ms: int
    slow_mo_ms: int
    user_agent: str | None
    browser_args: tuple[str, ...]
    viewport_width: int
    viewport_height: int

    #: The marketplace zone Amazon's Today/Yesterday presets are computed in.
    report_timezone: ZoneInfo
    #: Prefix of the unique name each created report is given, so the run can
    #: find its own report in the list among everyone else's.
    report_name_prefix: str
    #: How long a created report may take to reach "Completed" on Amazon.
    report_ready_timeout_seconds: int
    #: How often the report list is reloaded while waiting.
    report_poll_seconds: int
    download_timeout_ms: int

    table: str
    date_column: str
    date_format: str

    max_retries: int
    retry_backoff_seconds: float
    retry_backoff_max_seconds: float
    #: How long a run waits for another run's lock before giving up.
    lock_wait_seconds: int

    create_table: bool
    retention_days: int
    log_level: str
    log_retention_days: int
    keep_processed_files: bool = False

    @property
    def has_credentials(self) -> bool:
        """Whether an automatic sign-in can even be attempted."""
        return bool(self.amazon_email and self.amazon_password)

    @property
    def can_auto_login(self) -> bool:
        """Whether AdProduct should fill the sign-in form on an expired session."""
        return self.auto_login and self.has_credentials

    @property
    def reports_url_with_entity(self) -> str:
        """Sponsored ads reports URL carrying the configured advertiser entity."""
        if "entityId=" in self.reports_url:
            return self.reports_url
        separator = "&" if "?" in self.reports_url else "?"
        return f"{self.reports_url}{separator}entityId={self.entity_id}"

    def ensure_directories(self) -> None:
        """Create every directory AdProduct writes to.  Idempotent."""
        for path in (
            self.download_dir,
            self.processed_dir,
            self.failed_dir,
            self.log_dir,
            self.screenshot_dir,
            self.session_dir,
            self.lock_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, object]:
        """Loggable snapshot of the configuration.  Contains no secrets."""
        return {
            "reports_url": self.reports_url,
            "entity_id": self.entity_id,
            # The address itself is not logged; only whether a sign-in could be
            # attempted without a person.
            "credentials": "configured" if self.has_credentials else "not configured",
            "auto_login": self.can_auto_login,
            "database": self.database.safe_url,
            "table": self.table,
            "download_dir": str(self.download_dir),
            "log_dir": str(self.log_dir),
            "session_dir": str(self.session_dir),
            "headless": self.headless,
            "report_timezone": str(self.report_timezone),
            "report_ready_timeout": f"{self.report_ready_timeout_seconds}s",
            "max_retries": self.max_retries,
            "keep_processed": self.keep_processed_files,
        }


def load_settings(env_file: Path | str | None = None, *, override: bool = False) -> Settings:
    """Read, validate and return the runtime configuration.

    Args:
        env_file: Optional explicit ``.env`` path.  Defaults to ``.env`` beside
            the project root when it exists.
        override: Whether ``.env`` values replace already-exported variables.
            Off by default so that real environment variables (systemd) win.

    Raises:
        ConfigurationError: If a required value is missing or malformed.
    """
    candidate = Path(env_file) if env_file else PROJECT_ROOT / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=override)
    elif env_file is not None:
        raise ConfigurationError(f"env file not found: {candidate}")

    data_dir = PROJECT_ROOT / "data"

    ssl_ca_raw = _get("DB_SSL_CA")
    ssl_ca = None
    if ssl_ca_raw:
        ssl_ca = Path(ssl_ca_raw).expanduser()
        if not ssl_ca.is_absolute():
            ssl_ca = (PROJECT_ROOT / ssl_ca).resolve()
        if not ssl_ca.is_file():
            raise ConfigurationError(f"DB_SSL_CA file not found: {ssl_ca}")

    database = DatabaseConfig(
        host=_require("DB_HOST"),
        port=_get_int("DB_PORT", 3306, minimum=1),
        name=_require("DB_NAME"),
        user=_require("DB_USER"),
        password=_require("DB_PASSWORD"),
        pool_recycle_seconds=_get_int("DB_POOL_RECYCLE_SECONDS", 1800, minimum=60),
        connect_timeout_seconds=_get_int("DB_CONNECT_TIMEOUT_SECONDS", 30, minimum=1),
        ssl_ca=ssl_ca,
        ssl_verify_cert=_get_bool("DB_SSL_VERIFY_CERT", False),
        ssl_disabled=_get_bool("DB_SSL_DISABLED", False),
    )

    url = _get("AMAZON_REPORTS_URL", DEFAULT_REPORTS_URL) or DEFAULT_REPORTS_URL
    if not url.startswith("https://"):
        raise ConfigurationError(f"AMAZON_REPORTS_URL must be an https URL, got {url!r}")

    log_level = (_get("LOG_LEVEL", "INFO") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(f"LOG_LEVEL must be a standard level, got {log_level!r}")

    settings = Settings(
        reports_url=url,
        entity_id=_require("AMAZON_ENTITY_ID"),
        amazon_email=_get("AMAZON_EMAIL"),
        amazon_password=_get("AMAZON_PASSWORD"),
        auto_login=_get_bool("AMAZON_AUTO_LOGIN", True),
        database=database,
        download_dir=_get_path("DOWNLOAD_DIR", data_dir / "downloads"),
        processed_dir=_get_path("PROCESSED_DIR", data_dir / "processed"),
        failed_dir=_get_path("FAILED_DIR", data_dir / "failed"),
        log_dir=_get_path("LOG_DIR", PROJECT_ROOT / "logs"),
        screenshot_dir=_get_path("SCREENSHOT_DIR", data_dir / "screenshots"),
        session_dir=_get_path("SESSION_DIR", data_dir / "session"),
        lock_file=_get_path("LOCK_FILE", data_dir / "adproduct.lock"),
        headless=_get_bool("HEADLESS", True),
        browser_timeout_ms=_get_int("BROWSER_TIMEOUT_MS", 60_000, minimum=1_000),
        slow_mo_ms=_get_int("SLOW_MO_MS", 0, minimum=0),
        user_agent=_get("BROWSER_USER_AGENT"),
        browser_args=_browser_args(),
        viewport_width=_get_int("VIEWPORT_WIDTH", 1920, minimum=320),
        viewport_height=_get_int("VIEWPORT_HEIGHT", 1080, minimum=320),
        report_timezone=_get_zone("REPORT_TIMEZONE", "US/Pacific"),
        report_name_prefix=_get("REPORT_NAME_PREFIX", "AdProduct") or "AdProduct",
        report_ready_timeout_seconds=_get_int("REPORT_READY_TIMEOUT_SECONDS", 5_400, minimum=30),
        report_poll_seconds=_get_int("REPORT_POLL_SECONDS", 20, minimum=5),
        download_timeout_ms=_get_int("DOWNLOAD_TIMEOUT_MS", 180_000, minimum=1_000),
        table=_get("DB_TABLE", DEFAULT_TABLE) or DEFAULT_TABLE,
        date_column=_get("DB_DATE_COLUMN", "Date") or "Date",
        date_format=_get("DB_DATE_FORMAT", "%Y-%m-%d") or "%Y-%m-%d",
        max_retries=_get_int("MAX_RETRIES", 3, minimum=1),
        retry_backoff_seconds=_get_float("RETRY_BACKOFF_SECONDS", 10.0, minimum=0.0),
        retry_backoff_max_seconds=_get_float("RETRY_BACKOFF_MAX_SECONDS", 120.0, minimum=0.0),
        lock_wait_seconds=_get_int("LOCK_WAIT_SECONDS", 3_600, minimum=0),
        create_table=_get_bool("CREATE_TABLE", False),
        retention_days=_get_int("FILE_RETENTION_DAYS", 30, minimum=1),
        log_level=log_level,
        log_retention_days=_get_int("LOG_RETENTION_DAYS", 5, minimum=1),
        keep_processed_files=_get_bool("KEEP_PROCESSED_FILES", False),
    )

    try:
        probe = __import__("datetime").date(2026, 1, 31).strftime(settings.date_format)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(
            f"DB_DATE_FORMAT={settings.date_format!r} is not a valid strftime format"
        ) from exc
    if probe == settings.date_format:
        raise ConfigurationError(
            f"DB_DATE_FORMAT={settings.date_format!r} contains no date directives "
            "(expected something like %Y-%m-%d)"
        )

    if bool(settings.amazon_email) != bool(settings.amazon_password):
        missing = "AMAZON_PASSWORD" if settings.amazon_email else "AMAZON_EMAIL"
        raise ConfigurationError(
            f"{missing} is not set. Automatic sign-in needs both AMAZON_EMAIL and "
            "AMAZON_PASSWORD, or neither (session-only mode)."
        )
    return settings
