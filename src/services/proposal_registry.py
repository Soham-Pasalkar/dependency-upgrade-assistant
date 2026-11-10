"""Server-side Proposal Registry for lifecycle, provenance, and authenticity tracking.

Guarantees:
- Server assigns cryptographically random proposal IDs.
- Complete proposal content (original, proposed, diff, plan items) is retained in server memory.
- Apply requests reference the proposal ID; clients cannot forge or tamper with proposal contents.
- Strict atomic lifecycle state transitions (AVAILABLE -> APPLYING -> CONSUMED / ABANDONED).
- Concurrency-safe: two simultaneous requests using the same proposal_id can never both apply.
- Automatic TTL expiration and consumption tracking (preventing replay / double-apply).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from src.models.plan import UpgradeProposalResponse

logger = logging.getLogger(__name__)

DEFAULT_PROPOSAL_TTL_SECONDS = 3600.0  # 1 hour
DEFAULT_MAX_PROPOSALS = 1000


class ProposalLifecycleState(str, Enum):
    """Lifecycle states for a server-registered proposal."""
    AVAILABLE = "AVAILABLE"      # Freshly registered, ready to be claimed
    APPLYING = "APPLYING"        # Claimed by an in-flight mutation; locked against concurrency
    CONSUMED = "CONSUMED"        # Successfully applied to filesystem; cannot be re-applied
    ABANDONED = "ABANDONED"      # Failed during/after write; failed closed to prevent unsafe replay


class RegistryCapacityExceededError(RuntimeError):
    """Raised when proposal registry capacity is exhausted and all entries are active (APPLYING)."""
    pass


@dataclass
class ProposalClaimResult:
    """Result of an atomic proposal claim attempt."""
    success: bool
    status: str  # "OK", "PROPOSAL_NOT_FOUND", "PROPOSAL_EXPIRED", "PROPOSAL_ALREADY_IN_PROGRESS", "PROPOSAL_ALREADY_CONSUMED"
    message: str
    stored: Optional[StoredProposal] = None


@dataclass
class StoredProposal:
    """Represents a server-stored upgrade proposal with lifecycle metadata."""
    proposal_id: str
    proposal: UpgradeProposalResponse
    created_at: float
    state: ProposalLifecycleState = ProposalLifecycleState.AVAILABLE
    consumed_at: Optional[float] = None
    claim_thread_id: Optional[int] = None
    failure_reason: Optional[str] = None

    @property
    def consumed(self) -> bool:
        """Backward-compatible property: true if proposal has completed or failed closed."""
        return self.state in (ProposalLifecycleState.CONSUMED, ProposalLifecycleState.ABANDONED)

    @consumed.setter
    def consumed(self, value: bool) -> None:
        if value:
            self.state = ProposalLifecycleState.CONSUMED
        else:
            self.state = ProposalLifecycleState.AVAILABLE


class ProposalRegistry:
    """Thread-safe, bounded server-side proposal store with atomic lifecycle transitions."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_PROPOSAL_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_PROPOSALS,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._store: Dict[str, StoredProposal] = {}
        self._lock = threading.Lock()

    def store(self, proposal: UpgradeProposalResponse) -> str:
        """Register a synthesized proposal in the server store and assign a proposal_id.

        Capacity and Eviction Guarantees:
        - Never evicts a proposal in APPLYING state.
        - Prioritizes evicting CONSUMED or ABANDONED proposals first when over capacity.
        - If all non-APPLYING entries are exhausted, evicts oldest AVAILABLE proposal.
        - If all entries in the registry are in APPLYING state, raises RegistryCapacityExceededError.
        """
        with self._lock:
            self._evict_expired()

            # If over capacity, find non-APPLYING candidate for eviction
            if len(self._store) >= self.max_entries:
                evictable = [
                    k for k, v in self._store.items()
                    if v.state != ProposalLifecycleState.APPLYING
                ]
                if not evictable:
                    logger.error(
                        f"Cannot store new proposal: registry capacity ({self.max_entries}) exhausted "
                        f"and all entries are currently in APPLYING state."
                    )
                    raise RegistryCapacityExceededError(
                        f"Proposal registry capacity exhausted ({self.max_entries} entries); "
                        "all entries are actively in progress."
                    )

                # Prioritize evicting CONSUMED or ABANDONED proposals first
                completed = [
                    k for k in evictable
                    if self._store[k].state in (ProposalLifecycleState.CONSUMED, ProposalLifecycleState.ABANDONED)
                ]
                if completed:
                    oldest_id = min(completed, key=lambda k: self._store[k].created_at)
                else:
                    oldest_id = min(evictable, key=lambda k: self._store[k].created_at)

                del self._store[oldest_id]
                logger.info(f"Evicted proposal '{oldest_id}' to maintain capacity ({len(self._store)}/{self.max_entries})")

            proposal_id = f"prop_{uuid.uuid4().hex}"
            proposal.proposal_id = proposal_id

            stored = StoredProposal(
                proposal_id=proposal_id,
                proposal=proposal.model_copy(deep=True),
                created_at=time.time(),
                state=ProposalLifecycleState.AVAILABLE,
            )
            self._store[proposal_id] = stored
            logger.info(f"Registered proposal '{proposal_id}' (active entries: {len(self._store)})")
            return proposal_id

    def get(self, proposal_id: str) -> Optional[StoredProposal]:
        """Retrieve a stored proposal by ID if it exists and has not expired."""
        with self._lock:
            self._evict_expired()
            stored = self._store.get(proposal_id)
            if stored is None:
                return None
            return stored

    def claim_for_application(self, proposal_id: str) -> ProposalClaimResult:
        """Atomically claim a proposal for application.
        
        Transitions state from AVAILABLE to APPLYING under registry lock.
        Rejects concurrent claims with PROPOSAL_ALREADY_IN_PROGRESS or PROPOSAL_ALREADY_CONSUMED.
        """
        with self._lock:
            self._evict_expired()
            stored = self._store.get(proposal_id)
            if stored is None:
                return ProposalClaimResult(
                    success=False,
                    status="PROPOSAL_NOT_FOUND",
                    message=f"Proposal '{proposal_id}' was not found or has expired. Please re-generate the proposal.",
                    stored=None,
                )

            # 1. Active claim protection: APPLYING proposals NEVER expire by TTL while in-flight
            if stored.state == ProposalLifecycleState.APPLYING:
                return ProposalClaimResult(
                    success=False,
                    status="PROPOSAL_ALREADY_IN_PROGRESS",
                    message=f"Proposal '{proposal_id}' is currently being applied by another operation.",
                    stored=stored,
                )

            # 2. Check TTL expiration for inactive proposals (AVAILABLE, CONSUMED, ABANDONED)
            now = time.time()
            if (now - stored.created_at) > self.ttl_seconds:
                del self._store[proposal_id]
                return ProposalClaimResult(
                    success=False,
                    status="PROPOSAL_NOT_FOUND",
                    message=f"Proposal '{proposal_id}' has expired. Please re-generate the proposal.",
                    stored=None,
                )

            # 3. Check for non-claimable terminal states
            if stored.state in (ProposalLifecycleState.CONSUMED, ProposalLifecycleState.ABANDONED):
                return ProposalClaimResult(
                    success=False,
                    status="PROPOSAL_ALREADY_CONSUMED",
                    message=f"Proposal '{proposal_id}' has already been applied and consumed.",
                    stored=stored,
                )

            if stored.state != ProposalLifecycleState.AVAILABLE:
                return ProposalClaimResult(
                    success=False,
                    status="PROPOSAL_ALREADY_CONSUMED",
                    message=f"Proposal '{proposal_id}' is in non-claimable state '{stored.state}'.",
                    stored=stored,
                )

            # Atomically transition AVAILABLE -> APPLYING
            stored.state = ProposalLifecycleState.APPLYING
            stored.claim_thread_id = threading.get_ident()
            logger.info(f"Proposal '{proposal_id}' claimed for application by thread {stored.claim_thread_id}")
            return ProposalClaimResult(
                success=True,
                status="OK",
                message=f"Proposal '{proposal_id}' successfully claimed.",
                stored=stored,
            )

    def complete_application(self, proposal_id: str) -> bool:
        """Transition proposal from APPLYING to CONSUMED after verified filesystem write."""
        with self._lock:
            stored = self._store.get(proposal_id)
            if stored is None:
                logger.warning(f"Attempted to complete unknown proposal '{proposal_id}'.")
                return False
            if stored.state != ProposalLifecycleState.APPLYING:
                logger.warning(
                    f"Cannot complete proposal '{proposal_id}': state is '{stored.state}', expected APPLYING."
                )
                return False
            stored.state = ProposalLifecycleState.CONSUMED
            stored.consumed_at = time.time()
            logger.info(f"Proposal '{proposal_id}' marked as CONSUMED.")
            return True

    def release_application_claim(self, proposal_id: str) -> bool:
        """Release an application claim back to AVAILABLE when validation fails before any write."""
        with self._lock:
            stored = self._store.get(proposal_id)
            if stored is None:
                return False
            if stored.state != ProposalLifecycleState.APPLYING:
                logger.warning(
                    f"Cannot release claim for proposal '{proposal_id}': state is '{stored.state}', expected APPLYING."
                )
                return False
            stored.state = ProposalLifecycleState.AVAILABLE
            stored.claim_thread_id = None
            logger.info(f"Proposal '{proposal_id}' claim released; returned to AVAILABLE.")
            return True

    def fail_application(self, proposal_id: str, reason: str = "") -> bool:
        """Transition proposal to ABANDONED when write fails or crashes (fail closed, no replay)."""
        with self._lock:
            stored = self._store.get(proposal_id)
            if stored is None:
                return False
            if stored.state == ProposalLifecycleState.CONSUMED:
                logger.warning(f"Cannot fail proposal '{proposal_id}': proposal is already CONSUMED.")
                return False
            stored.state = ProposalLifecycleState.ABANDONED
            stored.failure_reason = reason
            stored.consumed_at = time.time()
            logger.warning(f"Proposal '{proposal_id}' failed closed (ABANDONED): {reason}")
            return True

    def mark_consumed(self, proposal_id: str) -> bool:
        """Mark proposal as consumed directly (for backward compatibility)."""
        with self._lock:
            stored = self._store.get(proposal_id)
            if stored is None or stored.consumed:
                return False
            stored.state = ProposalLifecycleState.CONSUMED
            stored.consumed_at = time.time()
            return True

    def clear(self) -> None:
        """Clear all entries (primarily for test cleanup)."""
        with self._lock:
            self._store.clear()

    def _evict_expired(self) -> None:
        """Remove entries exceeding TTL (never evicts active APPLYING proposals)."""
        now = time.time()
        expired_keys = [
            k for k, v in self._store.items()
            if (now - v.created_at) > self.ttl_seconds
            and v.state != ProposalLifecycleState.APPLYING
        ]
        for k in expired_keys:
            del self._store[k]


# Global singleton registry instance
proposal_registry = ProposalRegistry()
