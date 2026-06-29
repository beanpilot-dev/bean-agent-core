from agent_core.agent import PersonalFinanceAgent
from agent_core.workflow.tools import EXECUTION_TOOLS, MODEL_TOOLS


def _tool_name(tool) -> str:
    return getattr(tool, "name", "")


def test_model_tool_manifest_excludes_execution_tools() -> None:
    model_names = {_tool_name(tool) for tool in MODEL_TOOLS}
    execution_names = {_tool_name(tool) for tool in EXECUTION_TOOLS}

    assert execution_names
    assert model_names.isdisjoint(execution_names)
    assert "prepare_commit" in model_names
    assert "prepare_open" not in model_names
    assert "confirm_commit" not in model_names
    assert "confirm_bulk" not in model_names


def test_default_agent_uses_single_loop_manifest() -> None:
    agent = PersonalFinanceAgent()

    assert agent.model_tools == MODEL_TOOLS
    graph = agent.graph.get_graph()
    node_names = set(graph.nodes)
    assert "agent" in node_names
    assert "tools" in node_names
    assert "planner" not in node_names
    assert "synthesizer" not in node_names
