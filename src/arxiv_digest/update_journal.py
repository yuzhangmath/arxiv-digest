"""Application-facing access to the sole durable update receipt store."""
from arxiv_digest.update_runtime.protocol import (
    ExternalChangeAuthorization, FailureAuthorization, JournalSnapshot, JournalStore,
    NoInstallAuthorization, PendingReceiptError, PlanSnapshot, ProtectedPlanStore,
    ProtectedProvenanceStore, ProvenanceSnapshot, RecoveryAuthorization, ReplayAuthorization,
    StaleSnapshotError, StoreError, classify_journal,
)
