"""Approval-boundary contracts and host-controlled mutation dispatch."""

from typing import TYPE_CHECKING

from .contracts import PendingActionService, digest_payload

if TYPE_CHECKING:
    from .gateway import ToolExecutionGateway

__all__ = ["PendingActionService", "ToolExecutionGateway", "digest_payload"]


def __getattr__(name: str):
    if name == "ToolExecutionGateway":
        from .gateway import ToolExecutionGateway

        return ToolExecutionGateway
    raise AttributeError(name)
