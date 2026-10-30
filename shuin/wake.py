"""Wake a verifier blocked on the queue.

Enqueue, release, cancel, and a head change write one byte to a FIFO beside the ledger.
A waiting claim blocks in select on that FIFO, and looks at the queue again about once
a second when the byte was missed.
"""

import os
import select
from pathlib import Path

from shuin.errors import LedgerError


def fifo_path(key: str) -> Path:
    return Path(str(key) + ".wake")


def ensure_fifo(key: str) -> int:
    """Open the wake FIFO for reading. The caller closes the descriptor."""
    if not hasattr(os, "mkfifo"):
        raise LedgerError(400, "wake_unsupported", "Waiting on the queue needs a POSIX FIFO.")
    path = fifo_path(key)
    if path.exists() and not path.is_fifo():
        raise LedgerError(409, "wake_blocked", "Wake path is not a FIFO.")
    if not path.exists():
        try:
            os.mkfifo(path, 0o600)
        except FileExistsError:
            if not path.is_fifo():
                raise LedgerError(409, "wake_blocked", "Wake path is not a FIFO.") from None
    return os.open(path, os.O_RDWR | os.O_NONBLOCK)


def drain(fd: int) -> None:
    while True:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            return
        if not chunk:
            return


def notify(key: str) -> None:
    """Wake every waiter. A missing reader is fine: the row is already committed.

    A regular file at the wake path is left alone. --wait reports that itself.
    """
    path = fifo_path(key)
    if not path.exists() or not path.is_fifo():
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        os.write(fd, b"\n")
    except OSError:
        pass
    finally:
        os.close(fd)


def wait_fd(fd: int, timeout: float) -> bool:
    """Block until a wake byte arrives, or the timeout elapses. No spin."""
    if timeout < 0:
        timeout = 0
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return False
    drain(fd)
    return True
