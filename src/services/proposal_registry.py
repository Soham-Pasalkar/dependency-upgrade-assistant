"""Server-side Proposal Registry for lifecycle, provenance, and authenticity tracking.

Guarantees:
- Server assigns cryptographically random proposal IDs.
- Complete proposal content (original, proposed, diff, plan items) is retained in server memory.
- Apply requests reference the proposal ID; clients cannot forge or tamper with proposal contents.
- Automatic TTL expiration and consumption tracking (preventing replay / double-apply).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

from src.models.plan import UpgradeProposalResponse

logger = logging.getLogger(__name__)

DEFAULT_PROPOSAL_TTL_SECONDS = 3600.0  # 1 hour
DEFAULT_MAX_PROPOSALS = 1000


@dataclass
class StoredProposal:
    """Represents a server-stored upgrade proposal with lifecycle metadata."""
    proposal_id: str
    proposal: UpgradeProposalResponse
    created_at: float
    consumed: bool = False
    consumed_at: Optional[float] = None


class ProposalRegistry:
    """Thread-safe, bounded server-side proposal store with TTL expiration."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_PROPOSAL_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_PROPOSALS,
    ) -> None:
        self.ttl_seconds = max(10.0, float(ttl_seconds))
        self.max_entries = max(10, int(max_entries))
        self._store: Dict[str, StoredProposal] = {}
        self._lock = threading.Lock()

    def store(self, proposal: UpgradeProposalResponse) -> str:
        """Register a synthesized proposal in the server store and assign a proposal_id."""
        with self._lock:
            self._evict_expired()
            # If over capacity, remove oldest
            if len(self._store) >= self.max_entries:
                oldest_id = min(self._store.keys(), key=lambda k: self._store[k].created_at)
                del self._store[oldest_id]

            proposal_id = f"prop_{uuid.uuid4().hex}"
            proposal.proposal_id = proposal_id

            stored = StoredProposal(
                proposal_id=proposal_id,
                proposal=proposal.model_copy(deep=True),
                created_at=time.time(),
                consumed=False,
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

    def mark_consumed(self, proposal_id: str) -> bool:
        """Mark a proposal as consumed upon successful workspace mutation."""
        with self._lock:
            stored = self._store.get(proposal_id)
            if stored is None or stored.consumed:
                return False
            stored.consumed = True
            stored.consumed_at = time.time()
            return True

    def clear(self) -> None:
        """Clear all entries (primarily for test cleanup)."""
        with self._lock:
            self._store.clear()

    def _evict_expired(self) -> None:
        """Remove entries exceeding TTL."""
        now = time.time()
        expired_keys = [
            k for k, v in self._store.items()
            if (now - v.created_at) > self.ttl_seconds
        ]
        for k in expired_keys:
            del self._store[k]


# Global singleton registry instance
proposal_registry = ProposalRegistry()
