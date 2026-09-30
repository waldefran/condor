"""One-shot Pydantic AI calls keep provider failures visible to retry policy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from condor.acp.pydantic_ai_client import PydanticAIClient
from condor.brooks.llm_coordination import is_transient_role_error


def _client_that_raises(error: BaseException) -> PydanticAIClient:
    client = PydanticAIClient(model="ollama:test")

    def fail_iter(*args, **kwargs):
        raise error

    client._agent = SimpleNamespace(iter=fail_iter)
    client._request_semaphore = asyncio.Semaphore(1)
    return client


def test_one_shot_prompt_reraises_original_timeout():
    timeout = TimeoutError("provider read timed out")
    client = _client_that_raises(timeout)

    with pytest.raises(TimeoutError) as caught:
        asyncio.run(client.prompt("hello"))

    assert caught.value is timeout


def test_one_shot_prompt_preserves_transport_exception_and_cause():
    transport_cause = ConnectionResetError("socket reset")
    transport_error = RuntimeError("provider transport error")
    transport_error.__cause__ = transport_cause
    client = _client_that_raises(transport_error)

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(client.prompt("hello"))

    assert caught.value is transport_error
    assert caught.value.__cause__ is transport_cause
    assert is_transient_role_error(caught.value)
