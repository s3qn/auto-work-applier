"""One file lock shared by every process that runs the pipeline.

flock is per open file description, so two threads in the web app that each
open the file conflict correctly, and the kernel releases the lock when a
process dies: there is never a stale lock.
"""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager

from autojob.config import Config


class RunInProgress(Exception):
    """Another run holds the run lock."""


@contextmanager
def hold_run_lock(config: Config, *, attempts: int = 3, delay: float = 0.2):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(config.run_lock_path, "a")
    try:
        for attempt in range(attempts):
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if attempt == attempts - 1:
                    raise RunInProgress("Another run is in progress.") from None
                time.sleep(delay)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def run_active(config: Config) -> bool:
    """True if some process currently holds the run lock."""
    try:
        with hold_run_lock(config, attempts=1):
            return False
    except RunInProgress:
        return True
