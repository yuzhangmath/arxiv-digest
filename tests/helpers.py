"""Assertions for contracts shared by native platform test suites."""

import os
import stat
from pathlib import Path


def assert_private_file(path: Path) -> None:
    if os.name == "nt":
        from arxiv_digest.atomic import validate_private_file

        with path.open("rb") as handle:
            validate_private_file(handle.fileno())
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
