"""Compatibility façade for the approval-boundary tool gateway.

Use :mod:`agent_core.services.approvals.gateway` for new internal imports.
"""

from .approvals.gateway import ToolExecutionGateway

__all__ = ["ToolExecutionGateway"]
