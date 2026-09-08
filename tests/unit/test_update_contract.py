from __future__ import annotations

import json
from pathlib import Path
import tomllib

import pytest


def test_bootstrap_contract_is_manual_and_derives_only_canonical_urls() -> None:
    from arxiv_digest.update_contract import BOOTSTRAP_POLICY, release_urls

    assert BOOTSTRAP_POLICY == {
        "application_data_generation": 2,
        "automatic_update": False,
        "automatic_update_from": None,
        "channel": "prerelease",
        "platforms": ("darwin", "linux"),
        "product": "arxiv-digest",
        "schema_version": 1,
        "updater_protocol": 1,
        "version": "0.3.0",
    }
    assert release_urls("0.3.1") == {
        "manifest": (
            "https://github.com/yuzhangmath/arxiv-digest/releases/download/"
            "v0.3.1/UPDATE_MANIFEST.json"
        ),
        "notes": (
            "https://github.com/yuzhangmath/arxiv-digest/releases/tag/v0.3.1"
        ),
        "wheel_prefix": (
            "https://github.com/yuzhangmath/arxiv-digest/releases/download/"
            "v0.3.1/"
        ),
    }


def test_bootstrap_contract_cannot_be_reassigned() -> None:
    from arxiv_digest.update_contract import BOOTSTRAP_POLICY

    try:
        with pytest.raises(TypeError):
            BOOTSTRAP_POLICY["version"] = "9.9.9"  # type: ignore[index]
    finally:
        if BOOTSTRAP_POLICY["version"] != "0.3.0":
            BOOTSTRAP_POLICY["version"] = "0.3.0"  # type: ignore[index]


def test_bootstrap_contract_nested_values_are_immutable() -> None:
    from arxiv_digest.update_contract import BOOTSTRAP_POLICY

    platforms = BOOTSTRAP_POLICY["platforms"]
    original = tuple(platforms)  # type: ignore[arg-type]
    try:
        with pytest.raises(TypeError):
            platforms[0] = "windows"  # type: ignore[index]
    finally:
        if tuple(platforms) != original:  # type: ignore[arg-type]
            platforms[0] = original[0]  # type: ignore[index]


def test_canonical_version_rejects_leading_zero() -> None:
    from arxiv_digest.update_contract import canonical_version

    with pytest.raises(ValueError, match="not canonical"):
        canonical_version("00.3.1")


def test_canonical_version_rejects_non_ascii_digits() -> None:
    from arxiv_digest.update_contract import canonical_version

    with pytest.raises(ValueError, match="not canonical"):
        canonical_version("\u0660.3.1")


def test_canonical_version_requires_exact_triplet() -> None:
    from arxiv_digest.update_contract import canonical_version

    with pytest.raises(ValueError, match="not canonical"):
        canonical_version("0.3.1.0")


def test_canonical_version_rejects_ascii_non_digits() -> None:
    from arxiv_digest.update_contract import canonical_version

    with pytest.raises(ValueError, match="not canonical"):
        canonical_version("+0.3.1")


def test_canonical_version_rejects_non_string_values() -> None:
    from arxiv_digest.update_contract import canonical_version

    with pytest.raises(ValueError, match="not canonical"):
        canonical_version(None)  # type: ignore[arg-type]


def test_public_update_status_values_are_closed() -> None:
    from arxiv_digest.update_contract import PublicUpdateStatus

    assert {status.value for status in PublicUpdateStatus} == {
        "idle",
        "checking",
        "current",
        "manual_fallback",
        "available_manual",
        "available_automatic",
        "preparing",
    }


def test_update_job_phase_values_are_closed() -> None:
    from arxiv_digest.update_contract import UpdateJobPhase

    assert {phase.value for phase in UpdateJobPhase} == {
        "downloading",
        "verifying",
        "snapshotting_environment",
        "stopping_work",
        "backing_up",
        "preparing_recovery",
        "ready_to_restart",
        "restarting",
    }


def test_update_job_state_values_are_closed() -> None:
    from arxiv_digest.update_contract import UpdateJobState

    assert {state.value for state in UpdateJobState} == {
        "running",
        "ready_to_restart",
        "restarting",
        "failed",
        "canceled",
    }


def test_update_job_error_values_are_closed() -> None:
    from arxiv_digest.update_contract import UpdateJobError

    assert {error.value for error in UpdateJobError} == {
        "eligibility_changed",
        "download_failed",
        "verification_failed",
        "snapshot_failed",
        "work_did_not_stop",
        "backup_failed",
        "recovery_preparation_failed",
        "helper_failed",
        "handoff_not_acknowledged",
        "helper_cancel_failed",
        "helper_commit_aborted",
        "helper_commit_failed",
        "external_change_detected",
        "application_closing",
        "pending_update_receipt",
    }


def test_update_journal_state_values_are_closed() -> None:
    from arxiv_digest.update_contract import JournalState

    assert {state.value for state in JournalState} == {
        "prepared",
        "committed",
        "installing",
        "target_installed",
        "launching_target",
        "healthy_pending_commit",
        "rolling_back",
        "canceling_no_install",
        "complete",
        "aborted_no_mutation",
        "recovery_failed",
        "external_change_detected",
    }


def test_journal_subphase_values_are_closed() -> None:
    from arxiv_digest.update_contract import JournalSubphase

    assert {subphase.value for subphase in JournalSubphase} == {
        "package_restore_pending",
    }


def test_receipt_outcome_values_are_closed() -> None:
    from arxiv_digest.update_contract import ReceiptOutcome

    assert {outcome.value for outcome in ReceiptOutcome} == {
        "updated",
        "restored",
        "handoff_failed",
        "external_change_detected",
        "recovery_failed",
    }


def test_receipt_message_values_are_closed() -> None:
    from arxiv_digest.update_contract import ReceiptMessage

    assert {message.value for message in ReceiptMessage} == {
        "update_succeeded",
        "update_failed_restored",
        "handoff_failed",
        "external_change_detected",
        "recovery_failed",
    }


def test_shutdown_intent_values_are_closed() -> None:
    from arxiv_digest.update_contract import ShutdownIntent

    assert {intent.value for intent in ShutdownIntent} == {
        "quit",
        "update_restart",
    }


def test_protocol_caps_and_deadlines_are_frozen() -> None:
    from arxiv_digest import update_contract

    assert {
        "release_list_requests": update_contract.RELEASE_LIST_REQUEST_LIMIT,
        "release_records": update_contract.RELEASE_RECORD_LIMIT,
        "release_page_bytes": update_contract.RELEASE_PAGE_BYTE_LIMIT,
        "manifest_bytes": update_contract.MANIFEST_BYTE_LIMIT,
        "wheel_bytes": update_contract.WHEEL_BYTE_LIMIT,
        "metadata_member_bytes": update_contract.METADATA_MEMBER_BYTE_LIMIT,
        "central_directory_bytes": (
            update_contract.CENTRAL_DIRECTORY_METADATA_BYTE_LIMIT
        ),
        "compression_ratio": update_contract.MEMBER_COMPRESSION_RATIO_LIMIT,
        "private_log_bytes": update_contract.PRIVATE_LOG_BYTE_LIMIT,
        "lock_wait_seconds": update_contract.LOCK_WAIT_TIMEOUT_SECONDS,
        "ready_seconds": update_contract.READY_DEADLINE_SECONDS,
        "parent_exit_seconds": update_contract.PARENT_EXIT_TIMEOUT_SECONDS,
        "pipx_seconds": update_contract.PIPX_COMMAND_TIMEOUT_SECONDS,
        "self_check_seconds": update_contract.SELF_CHECK_TIMEOUT_SECONDS,
        "data_recovery_seconds": update_contract.DATA_RECOVERY_TIMEOUT_SECONDS,
        "health_seconds": update_contract.HEALTH_HANDSHAKE_TIMEOUT_SECONDS,
        "term_grace_seconds": update_contract.TERM_GRACE_SECONDS,
        "browser_marker_seconds": (
            update_contract.BROWSER_MARKER_LIFETIME_SECONDS
        ),
        "discovery_seconds": update_contract.DISCOVERY_DEADLINE_SECONDS,
    } == {
        "release_list_requests": 10,
        "release_records": 1_000,
        "release_page_bytes": 4 * 1024 * 1024,
        "manifest_bytes": 1 * 1024 * 1024,
        "wheel_bytes": 128 * 1024 * 1024,
        "metadata_member_bytes": 64 * 1024,
        "central_directory_bytes": 8 * 1024 * 1024,
        "compression_ratio": 100,
        "private_log_bytes": 4 * 1024 * 1024,
        "lock_wait_seconds": 45.0,
        "ready_seconds": 120.0,
        "parent_exit_seconds": 60.0,
        "pipx_seconds": 15.0 * 60.0,
        "self_check_seconds": 60.0,
        "data_recovery_seconds": 60.0,
        "health_seconds": 90.0,
        "term_grace_seconds": 10.0,
        "browser_marker_seconds": 24.0 * 60.0 * 60.0,
        "discovery_seconds": 60.0,
    }


def test_release_asset_transport_contract_is_frozen() -> None:
    from arxiv_digest import update_contract

    assert update_contract.RELEASE_ASSET_REDIRECT_LIMIT == 5
    assert update_contract.RELEASE_ASSET_HOSTS == (
        "github.com",
        "release-assets.githubusercontent.com",
    )
    assert update_contract.MANIFEST_MEDIA_TYPES == (
        "application/json",
        "application/octet-stream",
    )


def test_fixed_update_filenames_are_frozen() -> None:
    from arxiv_digest import update_contract

    assert {
        "manifest": update_contract.UPDATE_MANIFEST_FILENAME,
        "transition_lock": update_contract.TRANSITION_LOCK_FILENAME,
        "launcher_lock": update_contract.LAUNCHER_OPERATION_LOCK_FILENAME,
        "recovery_wrapper": update_contract.RECOVERY_WRAPPER_FILENAME,
    } == {
        "manifest": "UPDATE_MANIFEST.json",
        "transition_lock": "update-transition.lock",
        "launcher_lock": "launcher-operation.lock",
        "recovery_wrapper": "recover-arxiv-digest",
    }


def test_private_recovery_inventory_names_are_frozen() -> None:
    from arxiv_digest import update_contract

    assert {
        "root": update_contract.UPDATE_RECOVERY_DIRNAME,
        "snapshot_root": update_contract.UPDATE_SNAPSHOT_DIRNAME,
        "journal_lock": update_contract.UPDATE_JOURNAL_LOCK_FILENAME,
        "plan": update_contract.UPDATE_PLAN_FILENAME,
        "journal": update_contract.UPDATE_JOURNAL_FILENAME,
        "provenance": update_contract.UPDATE_PROVENANCE_FILENAME,
        "log": update_contract.UPDATE_DIAGNOSTIC_LOG_FILENAME,
        "runtime": update_contract.UPDATE_RUNTIME_DIRNAME,
    } == {
        "root": "update-recovery",
        "snapshot_root": "arxiv-digest-update-snapshots",
        "journal_lock": "update-journal.lock",
        "plan": "update-plan.json",
        "journal": "update-journal.json",
        "provenance": "update-provenance.json",
        "log": "update-diagnostic.log",
        "runtime": "runtime",
    }


def test_schema1_requirement_vectors_are_frozen() -> None:
    fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "update"
        / "schema1-requirements.json"
    )

    assert json.loads(fixture.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "valid": [
            {"values": [], "normalized": ""},
            {
                "values": [
                    "beautifulsoup4<5,>=4.12",
                    "packaging<27,>=24",
                ],
                "normalized": (
                    "beautifulsoup4<5,>=4.12\npackaging<27,>=24\n"
                ),
            },
            {
                "values": [
                    "demo; python_version >= '3.11'",
                    "demo; python_version >= '3.11'",
                ],
                "normalized": (
                    "demo; python_version >= '3.11'\n"
                    "demo; python_version >= '3.11'\n"
                ),
            },
        ],
        "invalid": [
            "",
            "bad requirement ???",
            "name\nInjected: value",
            "name\0",
        ],
    }


def test_updater_dependencies_are_deterministic() -> None:
    project_file = Path(__file__).parents[2] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))

    assert project["build-system"]["requires"] == [
        "setuptools==80.9.0",
        "wheel==0.45.1",
    ]
    assert project["project"]["dependencies"] == [
        "beautifulsoup4>=4.12,<5",
        "packaging>=24,<27",
    ]
    assert project["project"]["optional-dependencies"]["dev"] == [
        "build>=1.2,<2",
        "pipx==1.16.7",
        "playwright>=1.46,<2",
        "pytest>=8,<9",
    ]
