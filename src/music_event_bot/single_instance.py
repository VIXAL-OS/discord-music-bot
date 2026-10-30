from __future__ import annotations

import os
from pathlib import Path
from typing import IO


def acquire_instance_lock(path: Path) -> IO[bytes] | None:
    """Hold an exclusive OS lock on ``path`` for the life of the process.

    Returns the open lock file, which the caller must keep referenced, or None
    when another process already holds the lock. Two bots on one database
    post every review card twice and fight over SQLite writes, and since the
    Task Scheduler job now retries every few minutes to revive a dead bot
    (it died silently on 2026-10-24 and stayed down six days), a bot started
    by hand would otherwise get a twin on the next tick. The OS drops the
    lock when the process exits, however it exits, so there is no stale
    pidfile to clean up after a crash or a kill.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle
