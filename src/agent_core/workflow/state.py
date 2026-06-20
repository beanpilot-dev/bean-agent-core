"""Shared state types for the workflow layer.

AgentState extends MessagesState with fields needed by the planner,
pillars, and synthesizer. PillarState is a narrower schema used by
pillar sub-graphs — it includes sub_task so pillars receive the
planner's task decomposition.
"""

from langgraph.graph import MessagesState


class AgentState(MessagesState):
    route: str
    sub_task: str
    original_query: str
    pending_routes: list[dict]
    had_multiple_tasks: bool
    preferred_language: str


class PillarState(MessagesState):
    sub_task: str
    preferred_language: str
