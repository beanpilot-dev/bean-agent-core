"""Compatibility façade for pending-action contracts.

Use :mod:`agent_core.services.approvals.contracts` for new internal imports.
"""

from .approvals.contracts import PendingActionService, digest_payload

__all__ = ["PendingActionService", "digest_payload"]
