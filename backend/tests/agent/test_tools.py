from agent.tools import TOOL_HANDLERS, TOOL_SCHEMAS


def test_route_planner_is_not_exposed_as_text_agent_tool():
    tool_names = {schema["function"]["name"] for schema in TOOL_SCHEMAS}

    assert "plan_route" not in tool_names
    assert "plan_route" not in TOOL_HANDLERS
