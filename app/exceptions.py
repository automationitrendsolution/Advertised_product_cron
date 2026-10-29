"""Exception hierarchy for AdProduct.

Every failure mode the pipeline can hit has its own type so that callers can
decide independently whether to retry, alert, or abort.  ``transient`` marks the
errors that are safe to retry; authentication/CAPTCHA/MFA failures are never
transient and must never be retried automatically.
"""

from __future__ import annotations


class AdProductError(Exception):
    """Base class for every AdProduct failure."""

    #: Whether retrying the same operation could plausibly succeed.
    transient: bool = False
    #: Process exit code used when this error reaches the top level.
    exit_code: int = 1


class ConfigurationError(AdProductError):
    """Missing or invalid environment configuration."""

    exit_code = 2


class AuthenticationError(AdProductError):
    """Amazon session is missing, expired, or blocked by a security challenge.

    Never retried: repeated automated attempts against sign-in, CAPTCHA, OTP or
    MFA are exactly what Amazon's protections exist to stop.  A human must
    re-establish the session with ``python run.py --login``.
    """

    exit_code = 3


class NavigationError(AdProductError):
    """The Sponsored ads reports page did not reach the expected state."""

    transient = True
    exit_code = 4


class ReportConfigError(AdProductError):
    """A report setting (category, type, time unit, period) could not be applied.

    Transient: most often a control that rendered late.  Each retry starts a
    fresh browser, and an abandoned "New report" form creates nothing.
    """

    transient = True
    exit_code = 5


class DownloadError(AdProductError):
    """A report never finished, failed on Amazon's side, or produced no file."""

    transient = True
    exit_code = 6


class ReportValidationError(AdProductError):
    """A downloaded file is not a usable report (wrong format, missing columns)."""

    exit_code = 7


class DatabaseError(AdProductError):
    """MySQL connectivity or transaction failure.

    ``transient`` is set per instance by :mod:`app.database`: a dropped
    connection or a lost lock race is retried, a schema or data error is not.
    """

    exit_code = 9


class EmptyReportError(ReportValidationError):
    """All three reports parsed correctly but held zero rows between them.

    Treated as a hard stop so that an empty export can never delete a good day
    of production data.
    """

    exit_code = 10


class LockError(AdProductError):
    """Another AdProduct run held the execution lock for too long."""

    exit_code = 11
