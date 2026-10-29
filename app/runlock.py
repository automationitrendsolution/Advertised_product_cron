"""Single-instance execution lock.

``flock`` on a dedicated file: the kernel releases it if the process is killed,
so a crashed run cannot wedge the schedule the way a stale PID file would.

Unlike a skip-if-busy lock, a run here *waits* (up to ``LOCK_WAIT_SECONDS``)
for the holder to finish.  The once-a-day "yesterday" run must not be lost
just because a 3-hourly "today" run happened to be in progress.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from .exceptions import LockError
from .logging_config import get_logger

logger = get_logger("lock")

#: How often a waiting run re-tries the lock.
_POLL_SECONDS = 5.0

if sys.platform == "win32":  # pragma: no cover - local development only
    import msvcrt

    def _try_lock(handle: IO[str]) -> bool:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[str]) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def run_lock(
    lock_file: Path,
    *,
    enabled: bool = True,
    wait_seconds: float = 0,
    sleep=time.sleep,
) -> Iterator[None]:
    """Hold an exclusive lock for the duration of the block.

    Args:
        wait_seconds: How long to keep trying while another run holds the
            lock.  ``0`` fails at once.

    Raises:
        LockError: If the lock is still held after ``wait_seconds``.
    """
    if not enabled:
        yield
        return

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+")
    try:
        deadline = time.monotonic() + wait_seconds
        announced = False
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                try:
                    handle.seek(0)
                    holder = handle.read().strip() or "unknown"
                except OSError:  # Windows refuses to read a locked region
                    holder = "unknown"
                raise LockError(
                    f"Another AdProduct run still holds {lock_file} (holder pid {holder}) "
                    f"after waiting {wait_seconds:.0f}s. Refusing to start a second run."
                )
            if not announced:
                logger.info(
                    "Another run holds %s; waiting up to %.0fs for it to finish",
                    lock_file,
                    wait_seconds,
                )
                announced = True
            sleep(_POLL_SECONDS)

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        logger.debug("Acquired run lock %s", lock_file)
        try:
            yield
        finally:
            _unlock(handle)
            logger.debug("Released run lock %s", lock_file)
    finally:
        handle.close()
