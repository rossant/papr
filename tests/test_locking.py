"""Exercise process exclusion with real flock operations and isolated state."""

import subprocess
import sys

import pytest

from papr.config import Config
from papr.locking import state_lock

CHILD = """
import sys
from papr import config
from papr.locking import state_lock
config.user_data_dir = lambda app: sys.argv[1]
try:
    with state_lock(config.Config(), timeout=0.1):
        pass
except TimeoutError:
    sys.exit(23)
"""


def test_nested_lock_excludes_other_process_and_releases_after_exception():
    config = Config()

    def attempt():
        return subprocess.run(
            [sys.executable, "-c", CHILD, str(config.data_dir)],
            capture_output=True,
            text=True,
            timeout=5,
        )

    with pytest.raises(RuntimeError, match="interrupted"):
        with state_lock(config):
            with state_lock(config):
                result = attempt()
                assert result.returncode == 23, result.stderr
            # Leaving the nested lock must not unlock the outer operation.
            assert attempt().returncode == 23
            raise RuntimeError("interrupted")
    result = attempt()
    assert result.returncode == 0, result.stderr
    assert (config.data_dir / ".state.lock").stat().st_mode & 0o777 == 0o600


def test_symlink_lock_file_is_rejected_without_changing_target(tmp_path):
    config = Config()
    config.data_dir.mkdir()
    target = tmp_path / "external"
    target.write_text("untouched")
    (config.data_dir / ".state.lock").symlink_to(target)
    with pytest.raises(OSError):
        with state_lock(config):
            pytest.fail("symlink lock must not be acquired")
    assert target.read_text() == "untouched"
