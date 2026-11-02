"""The shared run lock."""

from __future__ import annotations

import fcntl

import pytest

from autojob.pipeline.lock import RunInProgress, hold_run_lock, run_active


def test_lock_held_elsewhere_blocks_and_release_frees(config):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(config.run_lock_path, "a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert run_active(config) is True
        with pytest.raises(RunInProgress):
            with hold_run_lock(config, attempts=1):
                pass
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()

    assert run_active(config) is False
    with hold_run_lock(config, attempts=1):
        assert run_active(config) is True
