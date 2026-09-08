"""Application adapter to the authoritative copied-runtime recovery engine."""
from arxiv_digest.update_runtime.recovery import (
    SnapshotError, capture_partial_environment, replay_snapshot,
)

__all__ = ["SnapshotError", "capture_partial_environment", "replay_snapshot"]
