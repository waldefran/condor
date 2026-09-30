"""Focused prompt, reference-reader, and role-tool audit regression checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from condor.brooks import agent_runner
from condor.brooks.market_tools import (
    BROOKS_REFERENCE_MAX_CHARS,
    make_brooks_reference_tool,
)


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str


class _TraderCoverageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    context_timeframes_used: list[str]


class _Client:
    def __init__(self, responses, *, accepts_system_prompt=True, keeps_history=True):
        self.responses = iter(responses)
        self.accepts_system_prompt = accepts_system_prompt
        self.keeps_history = keeps_history
        self.prompts = []
        self.working_dir = None

    async def start(self):
        pass

    async def prompt(self, prompt):
        self.prompts.append(prompt)
        return next(self.responses)

    async def stop(self):
        pass


@pytest.mark.asyncio
async def test_runtime_prompts_have_one_role_specific_final_contract(monkeypatch):
    captured = {}
    client = _Client([json.dumps({"decision": "NO_TRADE"})])

    def build(*args, **kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(agent_runner, "build_llm_client", build)
    await agent_runner.run_role(
        "TRADER",
        agent_key="test-backend",
        prompt={"symbol": "BTC-USDT", "decision_time_ms": 100},
        output_model=_Output,
        market_tools={},
    )
    trader_system = captured["system_prompt"]
    assert "Al Brooks" in trader_system
    assert "TradeIntentV2" in trader_system
    assert "ENTER_LONG" in trader_system and "ENTER_SHORT" in trader_system
    assert "NO_TRADE" in trader_system
    assert "brooks.market-context.v2" not in trader_system
    assert '"market_context": {' not in trader_system

    captured.clear()
    client = _Client([json.dumps({"decision": "NO_TRADE"})])
    monkeypatch.setattr(
        agent_runner,
        "build_llm_client",
        lambda *args, **kwargs: (captured.update(kwargs) or client),
    )
    await agent_runner.run_role(
        "CONTEXT_ANALYST",
        agent_key="test-backend",
        prompt={
            "symbol": "BTC-USDT",
            "timeframe": "4h",
            "decision_time_ms": 100,
        },
        output_model=_Output,
        market_tools={},
    )
    context_system = captured["system_prompt"]
    assert "Al Brooks" in context_system
    assert "MarketContextV2" in context_system
    assert "brooks.market-context.v2" in context_system
    assert "TradeIntentV2" not in context_system
    assert "ENTER_LONG" not in context_system
    assert "ENTER_SHORT" not in context_system
    assert "NO_TRADE" not in context_system


def test_runtime_is_shorter_than_full_skills_and_context_has_no_trade_schema():
    skills = Path(__file__).resolve().parents[1] / "agents" / "brooks_price_action" / "skills"
    trader_runtime = (skills / "brooks-trade-entry" / "RUNTIME.md").read_text()
    context_runtime = (skills / "brooks-market-context" / "RUNTIME.md").read_text()
    old_trader_source = "\n".join(
        (
            (skills / "brooks-market-context" / "SKILL.md").read_text(),
            (skills / "brooks-trade-entry" / "SKILL.md").read_text(),
        )
    )
    assert len(trader_runtime) < len(old_trader_source)
    assert "get_market_context" not in trader_runtime
    assert "TradeIntentV2" not in context_runtime
    assert "brooks.trade-intent.v2" not in context_runtime
    for forbidden in (
        "preferred_side",
        "trade_bias",
        "recommended_entry",
        "probability_up",
        "probability_down",
    ):
        assert forbidden not in context_runtime


@pytest.mark.asyncio
async def test_trader_exposes_only_four_read_tools(monkeypatch):
    client = _Client([json.dumps({"decision": "NO_TRADE"})])
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *args, **kwargs: client)
    await agent_runner.run_role(
        "TRADER",
        agent_key="test-backend",
        prompt={"symbol": "BTC-USDT", "decision_time_ms": 100},
        output_model=_Output,
        market_tools={
            "get_closed_candles": lambda: [],
            "get_recent_structure": lambda: {},
            "get_volatility": lambda: {},
        },
    )
    assert client.prompts[0].splitlines()[0] == (
        "Allowed read tools: get_closed_candles, get_recent_structure, "
        "get_volatility, read_brooks_reference"
    )
    with pytest.raises(ValueError, match="tools forbidden"):
        await agent_runner.run_role(
            "TRADER",
            agent_key="test-backend",
            prompt={"symbol": "BTC-USDT"},
            output_model=_Output,
            market_tools={"get_market_context": lambda: None},
        )


@pytest.mark.asyncio
async def test_trader_repairs_missing_timeframe_coverage_once_with_frozen_input(monkeypatch):
    client = _Client(
        [
            json.dumps({"decision": "NO_TRADE", "context_timeframes_used": ["H1"]}),
            json.dumps({"decision": "NO_TRADE", "context_timeframes_used": ["H1", "M15"]}),
        ]
    )
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *args, **kwargs: client)
    packet = {"symbol": "ETH-USDT", "decision_time_ms": 100, "raw": {"H1": [1], "M15": [2]}}
    result = await agent_runner.run_role(
        "TRADER",
        packet,
        _TraderCoverageOutput,
        {},
        agent_key="test-backend",
        max_tool_calls=0,
    )
    assert result.context_timeframes_used == ["H1", "M15"]
    assert len(client.prompts) == 2
    assert "\"decision_time_ms\": 100" in client.prompts[0]
    assert "M15" in client.prompts[1]

    bad_client = _Client(
        [json.dumps({"decision": "NO_TRADE", "context_timeframes_used": ["H1"]})] * 2
    )
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *args, **kwargs: bad_client)
    with pytest.raises(agent_runner.RoleRunError, match="timeframe coverage"):
        await agent_runner.run_role(
            "TRADER", packet, _TraderCoverageOutput, {},
            agent_key="test-backend", max_tool_calls=0,
        )
    assert len(bad_client.prompts) == 2


@pytest.mark.asyncio
async def test_runner_repairs_malformed_json_before_dispatching_a_read_tool(
    monkeypatch,
):
    dsml = '<tool name="get_closed_candles"><arg name="limit">2</arg></tool>'
    tool_request = json.dumps(
        {
            "tool": "get_closed_candles",
            "arguments": {"symbol": "ETH-USDT", "timeframe": "15m", "limit": 2},
        }
    )
    client = _Client(
        [dsml, tool_request, json.dumps({"decision": "NO_TRADE"})],
        accepts_system_prompt=False,
        keeps_history=False,
    )
    monkeypatch.setattr(
        agent_runner, "build_llm_client", lambda *args, **kwargs: client
    )
    read_calls = []
    write_calls = []

    def read_candles(**arguments):
        read_calls.append(arguments)
        return [{"close": "2638"}]

    packet = {
        "symbol": "ETH-USDT",
        "decision_time_ms": 100,
        "raw": {"M15": [{"close": "2638"}]},
    }
    original_packet = json.loads(json.dumps(packet))
    audit = []
    result = await agent_runner.run_role(
        "TRADER",
        packet,
        _Output,
        {"get_closed_candles": read_candles},
        agent_key="test-backend",
        max_tool_calls=1,
        tool_audit=audit,
    )

    assert result.decision == "NO_TRADE"
    assert packet == original_packet
    assert len(client.prompts) == 3
    assert "Input: " in client.prompts[1]
    assert '"decision_time_ms": 100' in client.prompts[1]
    assert "same frozen input" in client.prompts[1]
    assert len(read_calls) == 1  # the DSML response never dispatched a tool
    assert write_calls == []
    assert audit[0]["type"] == "contract_repair"
    assert audit[0]["stage"] == "json_protocol"
    assert audit[0]["status"] == "requested"
    assert audit[1]["tool"] == "get_closed_candles"


@pytest.mark.asyncio
async def test_runner_repairs_schema_error_and_output_validator_error(monkeypatch):
    schema_client = _Client(
        [json.dumps({"extra": "ambiguous"}), json.dumps({"decision": "NO_TRADE"})]
    )
    monkeypatch.setattr(
        agent_runner, "build_llm_client", lambda *args, **kwargs: schema_client
    )
    schema_audit = []
    result = await agent_runner.run_role(
        "HTF_ANALYST",
        {"symbol": "ETH-USDT", "decision_time_ms": 100},
        _Output,
        {},
        agent_key="test-backend",
        tool_audit=schema_audit,
    )
    assert result.decision == "NO_TRADE"
    assert len(schema_client.prompts) == 2
    assert schema_audit[0]["type"] == "contract_repair"
    assert schema_audit[0]["stage"] == "schema_validation"
    assert "same frozen input" in schema_client.prompts[1]

    validator_client = _Client(
        [json.dumps({"decision": "NO_TRADE"}), json.dumps({"decision": "NO_TRADE"})]
    )
    monkeypatch.setattr(
        agent_runner, "build_llm_client", lambda *args, **kwargs: validator_client
    )
    validation_calls = 0

    def validate_output(_output):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 1:
            raise ValueError("entry reference does not match an observed candle")

    validator_audit = []
    result = await agent_runner.run_role(
        "TRADER",
        {"symbol": "ETH-USDT", "decision_time_ms": 100},
        _Output,
        {},
        agent_key="test-backend",
        tool_audit=validator_audit,
        output_validator=validate_output,
    )
    assert result.decision == "NO_TRADE"
    assert validation_calls == 2
    assert validator_audit[0]["type"] == "contract_repair"
    assert validator_audit[0]["stage"] == "output_validation"


@pytest.mark.asyncio
async def test_runner_rejects_second_bad_contract_output_and_keeps_account_boundary(
    monkeypatch,
):
    client = _Client(["not JSON", "still not JSON"])
    monkeypatch.setattr(
        agent_runner, "build_llm_client", lambda *args, **kwargs: client
    )
    audit = []
    with pytest.raises(agent_runner.RoleRunError, match="invalid JSON") as caught:
        await agent_runner.run_role(
            "HTF_ANALYST",
            {"symbol": "ETH-USDT"},
            _Output,
            {},
            agent_key="test-backend",
            tool_audit=audit,
        )
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)
    assert len(client.prompts) == 2
    assert [record["status"] for record in audit] == ["requested", "rejected"]

    private_client = _Client(
        [json.dumps({"decision": "NO_TRADE", "account": {"equity": "1"}})]
    )
    monkeypatch.setattr(
        agent_runner, "build_llm_client", lambda *args, **kwargs: private_client
    )
    private_audit = []
    with pytest.raises(agent_runner.RoleRunError, match="private-data boundary"):
        await agent_runner.run_role(
            "TRADER",
            {"symbol": "ETH-USDT"},
            _Output,
            {},
            agent_key="test-backend",
            tool_audit=private_audit,
        )
    assert len(private_client.prompts) == 1
    assert private_audit == []


@pytest.mark.asyncio
async def test_opencode_and_system_channel_use_identical_runtime_instructions(monkeypatch):
    system_client = _Client([json.dumps({"decision": "NO_TRADE"})])
    system_config = {}

    def build_system(*args, **kwargs):
        system_config.update(kwargs)
        return system_client

    monkeypatch.setattr(agent_runner, "build_llm_client", build_system)
    await agent_runner.run_role(
        "TRADER",
        agent_key="pydantic-ai-backend",
        prompt={"symbol": "BTC-USDT"},
        output_model=_Output,
        market_tools={},
    )

    stateless_client = _Client(
        [json.dumps({"decision": "NO_TRADE"})],
        accepts_system_prompt=False,
        keeps_history=False,
    )
    stateless_config = {}

    def build_stateless(*args, **kwargs):
        stateless_config.update(kwargs)
        return stateless_client

    monkeypatch.setattr(agent_runner, "build_llm_client", build_stateless)
    await agent_runner.run_role(
        "TRADER",
        agent_key="opencode-go:model",
        prompt={"symbol": "BTC-USDT"},
        output_model=_Output,
        market_tools={},
    )
    assert stateless_config["system_prompt"] == system_config["system_prompt"]
    assert stateless_client.prompts[0].startswith(system_config["system_prompt"] + "\n\n")


def test_reference_reader_is_allowlisted_role_scoped_and_bounded():
    trader_reader = make_brooks_reference_tool("TRADER", max_chars=40)
    result = trader_reader("trade_entry.entry_evidence")
    assert result["resource"] == "trade_entry.entry_evidence"
    assert len(result["content"]) == 40
    assert result["truncated"] is True

    with pytest.raises(ValueError, match="unknown"):
        trader_reader("../brooks-trade-entry/SKILL.md")
    with pytest.raises(ValueError, match="unknown"):
        trader_reader("trade_entry.missing")
    with pytest.raises(PermissionError, match="unavailable"):
        make_brooks_reference_tool("CONTEXT_ANALYST")(
            "trade_entry.entry_evidence"
        )
    with pytest.raises(ValueError, match="max_chars"):
        make_brooks_reference_tool("TRADER", max_chars=BROOKS_REFERENCE_MAX_CHARS + 1)


@pytest.mark.asyncio
async def test_runner_audits_reference_calls_and_requires_raw_h4_for_stale_entry(
    monkeypatch,
):
    client = _Client(
        [
            json.dumps(
                {
                    "tool": "read_brooks_reference",
                    "arguments": {"resource": "trade_entry.entry_evidence"},
                }
            ),
            json.dumps({"decision": "NO_TRADE"}),
        ]
    )
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *args, **kwargs: client)
    audit = []
    result = await agent_runner.run_role(
        "TRADER",
        agent_key="test-backend",
        prompt={"symbol": "BTC-USDT", "macro_contexts": {"H4": {"freshness": "current"}}},
        output_model=_Output,
        market_tools={},
        tool_audit=audit,
    )
    assert result.decision == "NO_TRADE"
    assert audit == [
        {
            "role": "TRADER",
            "tool": "read_brooks_reference",
            "arguments": {"resource": "trade_entry.entry_evidence"},
            "status": "completed",
            "result_type": "dict",
            "result_count": 3,
        }
    ]

    stale_client = _Client([json.dumps({"decision": "ENTER_LONG"})])
    monkeypatch.setattr(
        agent_runner, "build_llm_client", lambda *args, **kwargs: stale_client
    )
    with pytest.raises(agent_runner.RoleRunError, match="same-run raw H4"):
        await agent_runner.run_role(
            "TRADER",
            agent_key="test-backend",
            prompt={
                "symbol": "BTC-USDT",
                "macro_contexts": {"H4": {"freshness": "stale"}},
            },
            output_model=_Output,
            market_tools={
                "get_closed_candles": lambda **kwargs: [
                    {"open": "1", "high": "2", "low": "1", "close": "2"}
                ]
            },
        )

    h4_client = _Client(
        [
            json.dumps(
                {
                    "tool": "get_closed_candles",
                    "arguments": {
                        "symbol": "BTC-USDT",
                        "timeframe": "H4",
                        "limit": 120,
                    },
                }
            ),
            json.dumps({"decision": "ENTER_LONG"}),
        ]
    )
    monkeypatch.setattr(agent_runner, "build_llm_client", lambda *args, **kwargs: h4_client)
    h4_audit = []
    entry = await agent_runner.run_role(
        "TRADER",
        agent_key="test-backend",
        prompt={
            "symbol": "BTC-USDT",
            "macro_contexts": {"H4": {"freshness": "missing"}},
        },
        output_model=_Output,
        market_tools={
            "get_closed_candles": lambda **kwargs: [
                {"open": "1", "high": "2", "low": "1", "close": "2"}
            ]
        },
        tool_audit=h4_audit,
    )
    assert entry.decision == "ENTER_LONG"
    assert h4_audit[0]["tool"] == "get_closed_candles"
    assert h4_audit[0]["status"] == "completed"
    assert h4_audit[0]["arguments"]["timeframe"] == "H4"
