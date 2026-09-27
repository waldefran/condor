"""Brooks role isolation and independent market consumers."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel, ConfigDict

from condor.brooks import agent_runner


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str


class _Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.prompts = []
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def prompt(self, prompt):
        self.prompts.append(prompt)
        return next(self.responses)

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_role_runner_uses_read_tools_and_validates_output(monkeypatch):
    client = _Client([
        json.dumps({"tool": "get_closed_candles", "arguments": {"limit": 2}}),
        json.dumps({"decision": "NO_TRADE"}),
    ])
    config = {}

    def make_client(*args, **kwargs):
        config.update(kwargs)
        return client

    monkeypatch.setattr(agent_runner, "build_llm_client", make_client)
    result = await agent_runner.run_role(
        "TRADER", agent_key="claude-acp:sonnet", context={"symbol": "BTC-USDT"},
        output_model=_Output,
        tools={"get_closed_candles": lambda limit: [{"close": "100"}] * limit},
    )
    assert result.decision == "NO_TRADE"
    assert client.started and client.stopped
    assert "Read tool get_closed_candles result" in client.prompts[1]
    assert config["mcp_servers"] == []
    assert config["allowed_tools"] == []
    assert (await config["permission_callback"]({}, []))["outcome"]["outcome"] == "cancelled"
    assert "brooks-trade-entry" in config["system_prompt"]


@pytest.mark.asyncio
async def test_role_runner_rejects_private_context_and_write_tools():
    with pytest.raises(ValueError, match="private"):
        await agent_runner.run_role(
            "TRADER", agent_key="claude-code", context={"account": {"equity": 1}},
            output_model=_Output, tools={},
        )
    with pytest.raises(ValueError, match="forbidden"):
        await agent_runner.run_role(
            "TRADER", agent_key="claude-code", context={}, output_model=_Output,
            tools={"create_order_executor": lambda: None},
        )


@pytest.mark.asyncio
async def test_role_runner_rejects_unknown_tool_request(monkeypatch):
    client = _Client([json.dumps({"tool": "get_position_state", "arguments": {}})])
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *a, **k: client)
    with pytest.raises(agent_runner.RoleRunError, match="unavailable"):
        await agent_runner.run_role(
            "HTF_ANALYST", agent_key="claude-code", context={"symbol": "BTC-USDT"},
            output_model=_Output, tools={},
        )
    assert client.stopped


@pytest.mark.asyncio
async def test_role_runner_timeout_stops_client(monkeypatch):
    class SlowClient(_Client):
        async def prompt(self, prompt):
            await asyncio.sleep(1)

    client = SlowClient([])
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *a, **k: client)
    with pytest.raises(TimeoutError):
        await agent_runner.run_role(
            "HTF_ANALYST", agent_key="claude-code", context={},
            output_model=_Output, tools={}, timeout_sec=0.01,
        )
    assert client.stopped
