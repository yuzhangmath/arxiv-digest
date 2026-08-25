#!/usr/bin/env python3
"""Verify that a release tag exactly matches the application version."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence

from arxiv_digest import __version__


class ReleaseIdentityError(ValueError):
    """Raised when a proposed release identity is inconsistent."""


_CANONICAL_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
)


def validate_release_tag(
    tag: str,
    *,
    application_version: str = __version__,
) -> str:
    if (
        not isinstance(application_version, str)
        or _CANONICAL_VERSION.fullmatch(application_version) is None
    ):
        raise ReleaseIdentityError("application version is not canonical")
    expected = f"v{application_version}"
    if tag != expected:
        raise ReleaseIdentityError(
            "release tag does not match application version"
        )
    return expected


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release_check.py",
        allow_abbrev=False,
    )
    parser.add_argument("--tag", required=True)
    arguments = parser.parse_args(argv)
    try:
        verified = validate_release_tag(arguments.tag)
    except ReleaseIdentityError as error:
        parser.error(str(error))
    print(f"release identity verified: {verified}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
