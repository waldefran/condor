"""Serialize Brooks role calls by the inference resource they share."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from copy import deepcopy
import itertools
import json
import errno
import threading
from typing import Any, AsyncIterator, Callable, Mapping, TypeVar
from weakref import WeakKeyDictionary

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
_HELD_BACKEND_KEYS: ContextVar[tuple[tuple[str, asyncio.Task[Any] | None], ...]] = ContextVar(
    "brooks_held_backend_keys", default=()
)


def is_transient_role_error(error: BaseException) -> bool:
    """Recognize bounded provider transport failures, excluding bad outputs."""
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, OSError) and error.errno in {
        errno.EAGAIN,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ETIMEDOUT,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
    }:
        return True
    names = {base.__name__.lower() for base in type(error).__mro__}
    if any(
        marker in name
        for name in names
        for marker in (
            "timeout",
            "connecterror",
            "connectionerror",
            "connectionreset",
            "transporterror",
            "networkerror",
            "ratelimiterror",
            "servererror",
            "serverdisconnected",
            "temporarilyunavailable",
            "serviceunavailable",
            "internalservererror",
        )
    ):
        return True
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "connection reset",
            "connection refused",
            "connection aborted",
            "connection error",
            "server disconnected",
            "network error",
            "timed out",
            "request timeout",
            "temporarily unavailable",
            "provider transport error",
            "transport error",
        )
    )


def _cached_tools(
    tools: Mapping[str, Callable[..., Any]], cache: dict[str, Any]
) -> dict[str, Callable[..., Any]]:
    """Cache successful read responses so retries see the same tool values."""
    import inspect

    result: dict[str, Callable[..., Any]] = {}
    for name, fn in tools.items():

        async def call(*args: Any, _name=name, _fn=fn, **kwargs: Any) -> Any:
            key = json.dumps([_name, args, kwargs], sort_keys=True, separators=(",", ":"), default=str)
            if key in cache:
                return deepcopy(cache[key])
            value = _fn(*args, **kwargs)
            if inspect.isawaitable(value):
                value = await value
            cache[key] = deepcopy(value)
            return deepcopy(value)

        result[name] = call
    return result


def backend_resource_key(agent_key: str) -> str:
    """Return the shared inference resource key for a Condor agent key.

    API model variants on one known provider share an endpoint. Named custom
    endpoints remain distinct. ACP and OpenCode keys stay exact because they
    can route through independent local runtimes or subscriptions.
    """
    key = (agent_key or "").strip()
    if not key:
        raise ValueError("agent_key is required to coordinate Brooks roles")

    if key.startswith("custom@"):
        endpoint, separator, _model = key.partition(":")
        return endpoint if separator else key

    provider, separator, _model = key.partition(":")
    shared_providers = {
        "anthropic",
        "bedrock",
        "cerebras",
        "cohere",
        "deepseek",
        "fireworks",
        "google-gla",
        "google-vertex",
        "groq",
        "lmstudio",
        "mistral",
        "ollama",
        "openai",
        "openrouter",
        "together",
        "xai",
    }
    if separator and provider in shared_providers:
        return provider
    return key


@dataclass(order=True)
class _Ticket:
    priority: int
    sequence: int
    task: asyncio.Task[Any] | None = field(compare=False, default=None)


class _PriorityGate:
    """One in-flight call per event loop and backend, with priority ordering."""

    def __init__(self) -> None:
        self.condition = asyncio.Condition()
        self.waiting: list[_Ticket] = []
        self.active = False

    @asynccontextmanager
    async def slot(self, priority: int) -> AsyncIterator[None]:
        ticket = _Ticket(priority, next(_SEQUENCES), asyncio.current_task())
        acquired = False
        async with self.condition:
            self.waiting.append(ticket)
            self.waiting.sort()
            try:
                while self.active or self.waiting[0] is not ticket:
                    await self.condition.wait()
                self.waiting.remove(ticket)
                self.active = True
                acquired = True
            except BaseException:
                if ticket in self.waiting:
                    self.waiting.remove(ticket)
                    self.condition.notify_all()
                raise
        try:
            yield
        finally:
            if acquired:
                async with self.condition:
                    self.active = False
                    self.condition.notify_all()


_SEQUENCES = itertools.count()
_GATES: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _PriorityGate]] = (
    WeakKeyDictionary()
)
_GATES_LOCK = threading.Lock()


def _gate_for(resource_key: str) -> _PriorityGate:
    loop = asyncio.get_running_loop()
    with _GATES_LOCK:
        loop_gates = _GATES.setdefault(loop, {})
        return loop_gates.setdefault(resource_key, _PriorityGate())


@asynccontextmanager
async def backend_call_slot(
    resource_key: str, *, priority: int
) -> AsyncIterator[None]:
    """Reserve a shared backend slot; smaller priorities run first."""
    if not resource_key:
        raise ValueError("backend resource key is required")
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError("priority must be an integer")
    held = _HELD_BACKEND_KEYS.get()
    current_task = asyncio.current_task()
    if any(key == resource_key and owner is current_task for key, owner in held):
        # A high-level consumer can reserve the backend across retry/backoff;
        # run_role's inner reservation then recognizes the same task context.
        yield
        return
    async with _gate_for(resource_key).slot(priority):
        token = _HELD_BACKEND_KEYS.set((*held, (resource_key, current_task)))
        try:
            yield
        finally:
            _HELD_BACKEND_KEYS.reset(token)


async def run_coordinated_role(
    role: str,
    prompt: Mapping[str, Any],
    output_model: type[T],
    market_tools: Mapping[str, Callable[..., Any]],
    *,
    agent_key: str,
    backend_key: str | None = None,
    priority: int = 10,
    runner: Callable[..., Any] | None = None,
    max_role_attempts: int = 2,
    retry_backoff_sec: float = 5,
    **run_options: Any,
) -> T:
    """Run a market role inside the shared backend's priority queue.

    Callers may pass ``runner`` to keep test adapters or instrumented runners;
    role options continue to use ``agent_runner.run_role``'s public signature.
    """
    if runner is None:
        from .agent_runner import run_role as runner

    if isinstance(max_role_attempts, bool) or not 1 <= max_role_attempts <= 2:
        raise ValueError("max_role_attempts must be 1 or 2")
    if retry_backoff_sec < 0:
        raise ValueError("retry_backoff_sec must be nonnegative")
    key = backend_key or backend_resource_key(agent_key)
    read_cache: dict[str, Any] = {}
    retry_tools = _cached_tools(market_tools, read_cache)
    frozen_prompt = deepcopy(dict(prompt))
    for attempt in range(1, max_role_attempts + 1):
        try:
            # Release the backend after a failed context attempt. A queued H1
            # Trader can then take its higher-priority turn before our retry.
            async with backend_call_slot(key, priority=priority):
                result = runner(
                    role,
                    deepcopy(frozen_prompt),
                    output_model,
                    retry_tools,
                    agent_key=agent_key,
                    backend_key=key,
                    priority=priority,
                    **run_options,
                )
                if hasattr(result, "__await__"):
                    result = await result
                return result
        except Exception as exc:
            if attempt >= max_role_attempts or not is_transient_role_error(exc):
                raise
            await asyncio.sleep(retry_backoff_sec)
    raise AssertionError("unreachable")
