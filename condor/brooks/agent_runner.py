"""Isolated Brooks roles on Condor's existing ACP/Pydantic-AI clients.

No regular Condor or Hummingbot MCP surface is mounted. Market reads use a
bounded JSON request/response loop, and native ACP tool requests are denied.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from condor.brooks.llm_coordination import backend_call_slot, backend_resource_key
from condor.brooks.market_tools import make_brooks_reference_tool
from condor.runtime.llm_client import build_llm_client

Role = Literal["TRADER", "CONTEXT_ANALYST", "HTF_ANALYST", "POSITION_MANAGER"]
OutputT = TypeVar("OutputT", bound=BaseModel)

_ROLE_RUNTIME_FILES: dict[Role, tuple[tuple[str, str], ...]] = {
    "TRADER": (("brooks-trade-entry", "RUNTIME.md"),),
    "CONTEXT_ANALYST": (("brooks-market-context", "RUNTIME.md"),),
    # PM keeps its existing full runtime until its prompt is deliberately
    # compacted as a separate change.
    "POSITION_MANAGER": (("brooks-position-management", "SKILL.md"),),
}
_ROLE_TOOLS: dict[Role, frozenset[str]] = {
    "TRADER": frozenset(
        {
            "get_closed_candles",
            "get_market_context",
            "get_recent_structure",
            "get_volatility",
            "read_brooks_reference",
        }
    ),
    "CONTEXT_ANALYST": frozenset(
        {
            "get_closed_candles",
            "get_recent_structure",
            "get_volatility",
            "read_brooks_reference",
        }
    ),
    "HTF_ANALYST": frozenset(
        {
            "get_closed_candles",
            "get_recent_structure",
            "get_volatility",
            "read_brooks_reference",
        }
    ),
    "POSITION_MANAGER": frozenset(
        {
            "get_position_state",
            "get_executor_state",
            "get_open_orders",
            "get_recent_fills",
            "get_original_trade_intent",
            "get_latest_trader_intent",
            "get_market_context",
            "get_recent_structure",
            "get_volatility",
            "get_candles",
            "read_brooks_reference",
        }
    ),
}
_SKILL_ROOT = (
    Path(__file__).resolve().parents[2] / "agents" / "brooks_price_action" / "skills"
)
_LEGACY_HTF_RUNTIME = """# Legacy HTF Analyst Runtime

Describe only the supplied closed-bar market structure at the given symbol,
timeframe, and decision time. Return the legacy `MarketContextV1` fields from
the supplied output schema directly as one JSON object: schema, role, symbol,
decision_time_ms, timeframe, observations, evidence_against, and uncertainty.
Do not wrap the result in a `market_context` object. Do not make any entry
recommendation, trade decision, preferred side, or probability. Never request
or emit account, position, order, fill, margin, target, stop, or size data.
"""


class RoleRunError(RuntimeError):
    """Model output or a tool request violated the Brooks role contract."""


def _normalize_key(key: Any) -> tuple[str, list[str]]:
    raw = str(key)
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()
    snake = re.sub(r"[_\-.\s]+", "_", s2)
    tokens = [t for t in snake.split("_") if t]
    return snake, tokens


_FORBIDDEN_TOKENS: frozenset[str] = frozenset(
    {
        "account",
        "balance",
        "equity",
        "margin",
        "position",
        "positions",
        "pnl",
        "order",
        "orders",
        "fill",
        "fills",
        "executor",
        "executors",
        "leverage",
        "hedge",
        "hedges",
    }
)
_FORBIDDEN_PHRASES: frozenset[str] = frozenset(
    {
        "trade_history",
        "management_history",
        "entry_price",
        "entry_prices",
        "available_margin",
        "available_balance",
        "unrealized_pnl",
        "realized_pnl",
        "position_id",
        "position_ids",
        "position_side",
        "position_size",
        "position_state",
        "open_orders",
        "open_positions",
    }
)


def _is_forbidden_key(key: Any) -> bool:
    snake, tokens = _normalize_key(key)
    if snake in _FORBIDDEN_PHRASES:
        return True
    if any(t in _FORBIDDEN_TOKENS for t in tokens):
        return True
    if {"trade", "history"}.issubset(tokens):
        return True
    if {"management", "history"}.issubset(tokens):
        return True
    if {"entry", "price"}.issubset(tokens) or {"entry", "prices"}.issubset(tokens):
        return True
    return False



def bind_symbol_tools(
    symbol: str,
    tools: Mapping[str, Callable[..., Any]],
) -> dict[str, Callable[..., Any]]:
    """Constrain role reads to the event symbol, including later tool requests."""
    if not symbol:
        raise ValueError("symbol required")
    bound = {}
    for name, fn in tools.items():

        async def call(*, _fn=fn, **kwargs):
            if kwargs.get("symbol") != symbol:
                raise RoleRunError("market tool symbol mismatch")
            result = _fn(**kwargs)
            return await result if inspect.isawaitable(result) else result

        bound[name] = call
    return bound


def role_skills(role: Role) -> str:
    """Load the role's runtime instructions, not the long-form source Skills."""
    if role == "HTF_ANALYST":
        return _LEGACY_HTF_RUNTIME
    try:
        files = _ROLE_RUNTIME_FILES[role]
    except KeyError as exc:
        raise ValueError(f"unknown Brooks role: {role}") from exc
    return "\n\n".join(
        (_SKILL_ROOT / name / filename).read_text(encoding="utf-8")
        for name, filename in files
    )


def _needs_h4_raw_verification(prompt: Mapping[str, Any]) -> bool:
    """Require an in-run H4 raw-bar read unless its supplied context is current.

    Prompts predating the macro-context packet retain their old behavior. Once
    the field is present, absent or malformed H4 freshness fails closed.
    """
    if "macro_contexts" not in prompt:
        return False
    contexts = prompt.get("macro_contexts")
    if not isinstance(contexts, Mapping):
        return True
    h4 = contexts.get("H4", contexts.get("h4"))
    if not isinstance(h4, Mapping):
        return True
    freshness = h4.get("freshness", h4.get("status"))
    return not isinstance(freshness, str) or freshness.lower() != "current"


def _has_successful_h4_raw_read(
    tool_audit: list[dict[str, Any]], *, symbol: Any
) -> bool:
    for call in tool_audit:
        if call.get("tool") != "get_closed_candles" or call.get("status") != "completed":
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, Mapping):
            continue
        timeframe = str(arguments.get("timeframe", "")).lower()
        if timeframe not in {"h4", "4h"}:
            continue
        if symbol is not None and arguments.get("symbol") != symbol:
            continue
        if call.get("result_type") == "list" and call.get("result_count", 0) > 0:
            return True
    return False


def _json_object(answer: str) -> dict[str, Any]:
    text = answer.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise RoleRunError("Brooks role returned invalid JSON") from exc
    if not isinstance(obj, dict):
        raise RoleRunError("Brooks role must return one JSON object")
    return obj


async def _deny_native_tool(_call: dict, _options: list[dict]) -> dict:
    return {"outcome": {"outcome": "cancelled"}}


async def run_role(
    role: Role,
    prompt: Mapping[str, Any],
    output_model: type[OutputT],
    market_tools: Mapping[str, Callable[..., Any]],
    *,
    agent_key: str,
    timeout_sec: float = 180,
    max_tool_calls: int = 8,
    user_id: int | None = None,
    tool_audit: list[dict[str, Any]] | None = None,
    backend_key: str | None = None,
    priority: int = 20,
) -> OutputT:
    """Run one role with read-only named tools and a validated final output.

    Callers must bind safe decision-time-bounded adapters. Only the role's names
    are accepted; the client retains reasoning history across tool requests.
    """
    if role not in _ROLE_TOOLS:
        raise ValueError(f"unknown Brooks role: {role}")
    if not agent_key:
        raise ValueError("agent_key is required")
    if timeout_sec <= 0 or max_tool_calls < 0:
        raise ValueError("invalid Brooks run budget")
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError("priority must be an integer")
    if tool_audit is not None and not isinstance(tool_audit, list):
        raise ValueError("tool_audit must be a mutable list")
    unknown = set(market_tools) - _ROLE_TOOLS[role]
    if unknown:
        raise ValueError(f"tools forbidden for {role}: {sorted(unknown)}")
    if role in ("TRADER", "CONTEXT_ANALYST", "HTF_ANALYST"):

        def check_public(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, child in value.items():
                    if _is_forbidden_key(key):
                        raise ValueError(
                            f"{role} prompt contains private account or position fields: {key}"
                        )
                    check_public(child)
            elif isinstance(value, (list, tuple, set)):
                for child in value:
                    check_public(child)

        check_public(prompt)
    instructions = (
        f"You are the independent Brooks {role} role. Use only supplied facts and "
        "the compact runtime below. Never invent data or request exchange writes. "
        "Reply with exactly one JSON object matching the supplied final output schema. "
        "To request a read tool, reply with "
        '{"tool":"name","arguments":{...}}; the host returns its result, then you '
        "may request another tool or return final JSON. Do not put JSON in prose.\n\n"
        + role_skills(role)
        + (
            "\n\nRead management evidence only through read_brooks_reference with "
            "resource `position_management.management_evidence`. Never request a path."
            if role == "POSITION_MANAGER"
            else ""
        )
    )
    client = build_llm_client(
        agent_key,
        mcp_servers=[],
        permission_callback=_deny_native_tool,
        allowed_tools=[],
        system_prompt=instructions,
        user_id=user_id,
    )
    run_tools = dict(market_tools)
    # The reference reader is host supplied, role scoped, and never accepts a
    # caller path. Mount it automatically for roles whose allowlist includes it.
    if "read_brooks_reference" in _ROLE_TOOLS[role] and "read_brooks_reference" not in run_tools:
        run_tools["read_brooks_reference"] = make_brooks_reference_tool(role)
    # Keep verification attempt-local even if the caller aggregates audit
    # records across retries for persistence.
    audit: list[dict[str, Any]] = []
    first_prompt = (
        f"Allowed read tools: {', '.join(sorted(run_tools)) or '(none)'}\n"
        f"Final output schema: {json.dumps(output_model.model_json_schema(), default=str)}\n"
        f"Input: {json.dumps(dict(prompt), default=str)}"
    )
    # The opencode CLI bridge has no system channel and one process per prompt:
    # inline the role instructions and resend the transcript each tool turn so
    # the model receives the same context a session-based backend keeps.
    keeps_history = bool(getattr(client, "keeps_history", True))
    if not getattr(client, "accepts_system_prompt", True):
        first_prompt = f"{instructions}\n\n{first_prompt}"
    # ACP bridges also discover .mcp.json from cwd. An empty mcpServers list is
    # insufficient while cwd is the Condor repository, whose file registers
    # Hummingbot and Condor servers. A fresh empty cwd removes that surface.
    resource_key = backend_key or backend_resource_key(agent_key)
    async with backend_call_slot(resource_key, priority=priority):
        with tempfile.TemporaryDirectory(prefix="condor-brooks-") as empty_cwd:
            if hasattr(client, "working_dir"):
                client.working_dir = empty_cwd
            async with asyncio.timeout(timeout_sec):
                try:
                    await client.start()
                    turn = first_prompt
                    for call_count in range(max_tool_calls + 1):
                        response = _json_object(await client.prompt(turn))
                        if "tool" not in response:
                            try:
                                output = output_model.model_validate(response)
                            except Exception as exc:
                                raise RoleRunError(
                                    "Brooks role output failed schema validation"
                                ) from exc
                            decision = getattr(output, "decision", None)
                            if (
                                role == "TRADER"
                                and decision in {"ENTER_LONG", "ENTER_SHORT"}
                                and _needs_h4_raw_verification(prompt)
                                and not _has_successful_h4_raw_read(
                                    audit, symbol=prompt.get("symbol")
                                )
                            ):
                                raise RoleRunError(
                                    "entry requires a successful same-run raw H4 candle read "
                                    "when H4 context is stale or missing"
                                )
                            return output
                        if call_count == max_tool_calls:
                            raise RoleRunError("Brooks role exceeded read tool budget")
                        if set(response) != {"tool", "arguments"}:
                            raise RoleRunError(
                                "tool request must contain only tool and arguments"
                            )
                        name, arguments = response["tool"], response["arguments"]
                        if not isinstance(name, str) or name not in run_tools:
                            raise RoleRunError(f"tool unavailable for {role}: {name}")
                        if not isinstance(arguments, dict):
                            raise RoleRunError("tool arguments must be a JSON object")
                        record = {
                            "role": role,
                            "tool": name,
                            "arguments": json.loads(json.dumps(arguments)),
                            "status": "started",
                        }
                        audit.append(record)
                        if tool_audit is not None:
                            tool_audit.append(record)
                        try:
                            result = run_tools[name](**arguments)
                            if inspect.isawaitable(result):
                                result = await result
                            payload = json.dumps(result, default=str)
                            record["status"] = "completed"
                            record["result_type"] = type(result).__name__
                            if isinstance(result, (list, tuple, dict, str)):
                                record["result_count"] = len(result)
                        except RoleRunError as exc:
                            record["status"] = "error"
                            record["error_type"] = type(exc).__name__
                            raise
                        except Exception as exc:
                            record["status"] = "error"
                            record["error_type"] = type(exc).__name__
                            payload = json.dumps(
                                {"error": f"Tool execution failed: {type(exc).__name__}: {exc}"}
                            )
                        reply = (
                            f"Read tool {name} result: {payload}\n"
                            "Continue. Request another allowed read tool or return final JSON."
                        )
                        if keeps_history:
                            turn = reply
                        else:
                            turn = (
                                f"{turn}\n"
                                f"Assistant tool request: "
                                f"{json.dumps(response, default=str)}\n"
                                f"{reply}"
                            )
                finally:
                    await client.stop()
    raise AssertionError("unreachable")
