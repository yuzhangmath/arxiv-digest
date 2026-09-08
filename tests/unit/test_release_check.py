from __future__ import annotations

import pytest

from arxiv_digest import __version__
from scripts.release_check import (
    ReleaseIdentityError,
    main,
    validate_release_tag,
)


def test_release_tag_must_exactly_match_the_application_version() -> None:
    assert (
        validate_release_tag("v0.2.1", application_version="0.2.1")
        == "v0.2.1"
    )


def test_release_tag_mismatch_is_rejected_without_echoing_the_candidate() -> None:
    with pytest.raises(
        ReleaseIdentityError,
        match="release tag does not match application version",
    ) as captured:
        validate_release_tag(
            "private-candidate",
            application_version="0.2.1",
        )

    assert "private-candidate" not in str(captured.value)


def test_noncanonical_application_version_is_rejected() -> None:
    with pytest.raises(
        ReleaseIdentityError,
        match="application version is not canonical",
    ):
        validate_release_tag("v0.2.01", application_version="0.2.01")


def test_non_text_application_version_is_rejected_safely() -> None:
    with pytest.raises(
        ReleaseIdentityError,
        match="application version is not canonical",
    ):
        validate_release_tag(
            "v0.2.1",
            application_version=None,  # type: ignore[arg-type]
        )


def test_cli_mismatch_does_not_echo_the_candidate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--tag", "private-candidate"])

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "release tag does not match application version" in captured.err
    assert "private-candidate" not in captured.err


def test_current_release_tag_is_accepted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--tag", f"v{__version__}"]) == 0
    assert capsys.readouterr().out == (
        f"release identity verified: v{__version__}\n"
    )
