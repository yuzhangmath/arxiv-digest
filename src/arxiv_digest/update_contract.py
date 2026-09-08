"""Closed constants shared by the release updater components."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from arxiv_digest.update_runtime.protocol import (
    PRIVATE_LOG_BYTE_LIMIT,
    LOCK_WAIT_TIMEOUT_SECONDS,
    READY_DEADLINE_SECONDS,
    PARENT_EXIT_TIMEOUT_SECONDS,
    PIPX_COMMAND_TIMEOUT_SECONDS,
    SELF_CHECK_TIMEOUT_SECONDS,
    DATA_RECOVERY_TIMEOUT_SECONDS,
    HEALTH_HANDSHAKE_TIMEOUT_SECONDS,
    TERM_GRACE_SECONDS,
)


class PublicUpdateStatus(StrEnum):
    """Release availability exposed by the update status endpoint."""

    IDLE = "idle"
    CHECKING = "checking"
    CURRENT = "current"
    MANUAL_FALLBACK = "manual_fallback"
    AVAILABLE_MANUAL = "available_manual"
    AVAILABLE_AUTOMATIC = "available_automatic"
    PREPARING = "preparing"


class UpdateJobPhase(StrEnum):
    """Public preparation and handoff progress phases."""

    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    SNAPSHOTTING_ENVIRONMENT = "snapshotting_environment"
    STOPPING_WORK = "stopping_work"
    BACKING_UP = "backing_up"
    PREPARING_RECOVERY = "preparing_recovery"
    READY_TO_RESTART = "ready_to_restart"
    RESTARTING = "restarting"


class UpdateJobState(StrEnum):
    """Public lifecycle states for a single update job."""

    RUNNING = "running"
    READY_TO_RESTART = "ready_to_restart"
    RESTARTING = "restarting"
    FAILED = "failed"
    CANCELED = "canceled"


class UpdateJobError(StrEnum):
    """Safe error codes exposed by update job and start responses."""

    ELIGIBILITY_CHANGED = "eligibility_changed"
    DOWNLOAD_FAILED = "download_failed"
    VERIFICATION_FAILED = "verification_failed"
    SNAPSHOT_FAILED = "snapshot_failed"
    WORK_DID_NOT_STOP = "work_did_not_stop"
    BACKUP_FAILED = "backup_failed"
    RECOVERY_PREPARATION_FAILED = "recovery_preparation_failed"
    HELPER_FAILED = "helper_failed"
    HANDOFF_NOT_ACKNOWLEDGED = "handoff_not_acknowledged"
    HELPER_CANCEL_FAILED = "helper_cancel_failed"
    HELPER_COMMIT_ABORTED = "helper_commit_aborted"
    HELPER_COMMIT_FAILED = "helper_commit_failed"
    EXTERNAL_CHANGE_DETECTED = "external_change_detected"
    APPLICATION_CLOSING = "application_closing"
    PENDING_UPDATE_RECEIPT = "pending_update_receipt"


class JournalState(StrEnum):
    """Durable update-transition journal states."""

    PREPARED = "prepared"
    COMMITTED = "committed"
    INSTALLING = "installing"
    TARGET_INSTALLED = "target_installed"
    LAUNCHING_TARGET = "launching_target"
    HEALTHY_PENDING_COMMIT = "healthy_pending_commit"
    ROLLING_BACK = "rolling_back"
    CANCELING_NO_INSTALL = "canceling_no_install"
    COMPLETE = "complete"
    ABORTED_NO_MUTATION = "aborted_no_mutation"
    RECOVERY_FAILED = "recovery_failed"
    EXTERNAL_CHANGE_DETECTED = "external_change_detected"


class JournalSubphase(StrEnum):
    """Durable replay boundary inside a journal state."""

    PACKAGE_RESTORE_PENDING = "package_restore_pending"


class ReceiptOutcome(StrEnum):
    """Terminal outcomes that may be acknowledged by the dashboard."""

    UPDATED = "updated"
    RESTORED = "restored"
    HANDOFF_FAILED = "handoff_failed"
    EXTERNAL_CHANGE_DETECTED = "external_change_detected"
    RECOVERY_FAILED = "recovery_failed"


class ReceiptMessage(StrEnum):
    """Fixed dashboard message codes carried by update receipts."""

    UPDATE_SUCCEEDED = "update_succeeded"
    UPDATE_FAILED_RESTORED = "update_failed_restored"
    HANDOFF_FAILED = "handoff_failed"
    EXTERNAL_CHANGE_DETECTED = "external_change_detected"
    RECOVERY_FAILED = "recovery_failed"


class ShutdownIntent(StrEnum):
    """First-writer-wins reasons for terminating the running server."""

    QUIT = "quit"
    UPDATE_RESTART = "update_restart"


PRODUCT = "arxiv-digest"
SCHEMA_VERSION = 1
UPDATER_PROTOCOL = 1
APPLICATION_DATA_GENERATION = 2
SUPPORTED_PLATFORMS = ("darwin", "linux")
ALLOWED_PIPX_VERSIONS = ("1.16.7",)
REPOSITORY = "https://github.com/yuzhangmath/arxiv-digest"

UPDATE_MANIFEST_FILENAME = "UPDATE_MANIFEST.json"
UPDATE_RECOVERY_DIRNAME = "update-recovery"
UPDATE_SNAPSHOT_DIRNAME = "arxiv-digest-update-snapshots"
TRANSITION_LOCK_FILENAME = "update-transition.lock"
LAUNCHER_OPERATION_LOCK_FILENAME = "launcher-operation.lock"
UPDATE_JOURNAL_LOCK_FILENAME = "update-journal.lock"
UPDATE_PLAN_FILENAME = "update-plan.json"
UPDATE_JOURNAL_FILENAME = "update-journal.json"
UPDATE_PROVENANCE_FILENAME = "update-provenance.json"
UPDATE_DIAGNOSTIC_LOG_FILENAME = "update-diagnostic.log"
UPDATE_RUNTIME_DIRNAME = "runtime"
RECOVERY_WRAPPER_FILENAME = "recover-arxiv-digest"

RELEASE_LIST_REQUEST_LIMIT = 10
RELEASE_RECORD_LIMIT = 1_000
RELEASE_PAGE_BYTE_LIMIT = 4 * 1024 * 1024
RELEASE_ASSET_REDIRECT_LIMIT = 5
RELEASE_ASSET_HOSTS = (
    "github.com",
    "release-assets.githubusercontent.com",
)
MANIFEST_MEDIA_TYPES = (
    "application/json",
    "application/octet-stream",
)
MANIFEST_BYTE_LIMIT = 1 * 1024 * 1024
WHEEL_BYTE_LIMIT = 128 * 1024 * 1024
METADATA_MEMBER_BYTE_LIMIT = 64 * 1024
CENTRAL_DIRECTORY_METADATA_BYTE_LIMIT = 8 * 1024 * 1024
MEMBER_COMPRESSION_RATIO_LIMIT = 100

DISCOVERY_DEADLINE_SECONDS = 60.0
BROWSER_MARKER_LIFETIME_SECONDS = 24.0 * 60.0 * 60.0

BOOTSTRAP_POLICY = MappingProxyType(
    {
        "application_data_generation": APPLICATION_DATA_GENERATION,
        "automatic_update": False,
        "automatic_update_from": None,
        "channel": "prerelease",
        "platforms": SUPPORTED_PLATFORMS,
        "product": PRODUCT,
        "schema_version": SCHEMA_VERSION,
        "updater_protocol": UPDATER_PROTOCOL,
        "version": "0.3.0",
    }
)


def canonical_version(value: str) -> tuple[int, int, int]:
    """Return numeric parts for an exact ASCII ``X.Y.Z`` release version."""

    if not isinstance(value, str):
        raise ValueError("application version is not canonical")
    parts = value.split(".")
    if len(parts) != 3 or any(
        not part.isascii()
        or not all("0" <= character <= "9" for character in part)
        or (len(part) > 1 and part.startswith("0"))
        for part in parts
    ):
        raise ValueError("application version is not canonical")
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch


def release_urls(version: str) -> dict[str, str]:
    """Derive canonical release asset URLs from a validated version."""

    canonical_version(version)
    base = f"{REPOSITORY}/releases"
    return {
        "manifest": f"{base}/download/v{version}/{UPDATE_MANIFEST_FILENAME}",
        "notes": f"{base}/tag/v{version}",
        "wheel_prefix": f"{base}/download/v{version}/",
    }
