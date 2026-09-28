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

from condor.runtime.llm_client import build_llm_client

Role = Literal["TRADER", "HTF_ANALYST", "POSITION_MANAGER"]
OutputT = TypeVar("OutputT", bound=BaseModel)

_ROLE_SKILLS: dict[Role, tuple[str, ...]] = {
    "TRADER": ("brooks-market-context", "brooks-trade-entry"),
    "HTF_ANALYST": ("brooks-market-context",),
    "POSITION_MANAGER": ("brooks-position-management",),
}
_ROLE_TOOLS: dict[Role, frozenset[str]] = {
    "TRADER": frozenset(
        {
            "get_closed_candles",
            "get_market_context",
            "get_recent_structure",
            "get_volatility",
        }
    ),
    "HTF_ANALYST": frozenset(
        {
            "get_closed_candles",
            "get_recent_structure",
            "get_volatility",
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
        }
    ),
}
_SKILL_ROOT = (
    Path(__file__).resolve().parents[2] / "agents" / "brooks_price_action" / "skills"
)


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
    """Load copied, versioned Brooks instructions from the stock agent package."""
    try:
        names = _ROLE_SKILLS[role]
    except KeyError as exc:
        raise ValueError(f"unknown Brooks role: {role}") from exc
    return "\n\n".join(
        (_SKILL_ROOT / name / "SKILL.md").read_text(encoding="utf-8") for name in names
    )


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
    unknown = set(market_tools) - _ROLE_TOOLS[role]
    if unknown:
        raise ValueError(f"tools forbidden for {role}: {sorted(unknown)}")
    if role in ("TRADER", "HTF_ANALYST"):

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
        "the copied Brooks skills below. Never invent data or request exchange writes. "
        "Reply with exactly one JSON object. To request a read tool, reply with "
        '{"tool":"name","arguments":{...}}; the host returns its result, then you '
        "may request another tool or return final JSON. Do not put JSON in prose.\n\n"
        + role_skills(role)
        + (
            "\n\nFor HTF_ANALYST, return the MarketContextV1 envelope from the final "
            "output schema. Summarize the skill's regime, phase and Always-In axes "
            "in observations and uncertainty; do not return its standalone "
            "market_context wrapper."
            if role == "HTF_ANALYST"
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
    first_prompt = (
        f"Allowed read tools: {', '.join(sorted(market_tools)) or '(none)'}\n"
        f"Final output schema: {json.dumps(output_model.model_json_schema(), default=str)}\n"
        f"Input: {json.dumps(dict(prompt), default=str)}"
    )
    # ACP bridges also discover .mcp.json from cwd. An empty mcpServers list is
    # insufficient while cwd is the Condor repository, whose file registers
    # Hummingbot and Condor servers. A fresh empty cwd removes that surface.
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
                            return output_model.model_validate(response)
                        except Exception as exc:
                            raise RoleRunError(
                                "Brooks role output failed schema validation"
                            ) from exc
                    if call_count == max_tool_calls:
                        raise RoleRunError("Brooks role exceeded read tool budget")
                    if set(response) != {"tool", "arguments"}:
                        raise RoleRunError(
                            "tool request must contain only tool and arguments"
                        )
                    name, arguments = response["tool"], response["arguments"]
                    if not isinstance(name, str) or name not in market_tools:
                        raise RoleRunError(f"tool unavailable for {role}: {name}")
                    if not isinstance(arguments, dict):
                        raise RoleRunError("tool arguments must be a JSON object")
                    try:
                        result = market_tools[name](**arguments)
                        if inspect.isawaitable(result):
                            result = await result
                        payload = json.dumps(result, default=str)
                    except RoleRunError:
                        raise
                    except Exception as exc:
                        payload = json.dumps(
                            {"error": f"Tool execution failed: {type(exc).__name__}: {exc}"}
                        )
                    turn = (
                        f"Read tool {name} result: {payload}\n"
                        "Continue. Request another allowed read tool or return final JSON."
                    )
            finally:
                await client.stop()
    raise AssertionError("unreachable")
