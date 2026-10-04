from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agent.tool_dispatch import TOOL_UPSTREAM_BUDGET_SECONDS, execute_tool_calls
from telemetry import get_telemetry
from upstream_deadline import remaining_budget


def test_each_tool_call_runs_under_an_upstream_budget():
    """A rider waits on every tool call, so upstream code sees a bounded budget."""
    seen: list[float | None] = []

    async def handler() -> str:
        seen.append(remaining_budget())
        return "ok"

    call = SimpleNamespace(id="c1", type="function", function=SimpleNamespace(name="probe", arguments="{}"))
    asyncio.run(execute_tool_calls([call], {"probe": handler}, get_telemetry()))

    assert seen and seen[0] is not None and 0 < seen[0] <= TOOL_UPSTREAM_BUDGET_SECONDS
    assert remaining_budget() is None, "the budget must not leak past the tool call"
