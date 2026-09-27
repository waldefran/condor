"""Headers for the OpenCode Go API (x-opencode-session + User-Agent)."""

import os

import pytest

from condor.acp.pydantic_ai_client import (
    OPENCODE_SESSION_HEADER,
    OPENCODE_USER_AGENT,
    PydanticAIClient,
    opencode_default_headers,
)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://opencode.ai/zen/go/v1",
        "https://opencode.ai/zen/go/v1/",
        "http://opencode.ai/zen/go/v1",
        "https://opencode.ai",
        "https://opencode.ai/",
        "https://go.opencode.ai/v1",
    ],
)
def test_helper_returns_both_headers_for_opencode_urls(base_url, monkeypatch):
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    headers = opencode_default_headers(base_url, session_id="sid-1")
    assert headers == {
        OPENCODE_SESSION_HEADER: "sid-1",
        "User-Agent": OPENCODE_USER_AGENT,
    }


@pytest.mark.parametrize(
    "base_url",
    [
        None,
        "",
        "http://localhost:11434/v1",
        "http://localhost:1234/v1",
        "https://api.openai.com/v1",
        "https://openrouter.ai/api/v1",
        "https://api.venice.ai/api/v1",
        "https://opencode.ai.evil.com/v1",
        "not-a-url",
    ],
)
def test_helper_returns_empty_for_non_opencode_urls(base_url, monkeypatch):
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    assert opencode_default_headers(base_url, session_id="sid-1") == {}


def test_env_override_honored(monkeypatch):
    monkeypatch.setenv("OPENCODE_SESSION_ID", "env-sid-123")
    headers = opencode_default_headers("https://opencode.ai/zen/go/v1")
    assert headers[OPENCODE_SESSION_HEADER] == "env-sid-123"
    # Explicit session id wins over the env var.
    headers = opencode_default_headers(
        "https://opencode.ai/zen/go/v1", session_id="explicit"
    )
    assert headers[OPENCODE_SESSION_HEADER] == "explicit"


def test_session_id_stable_per_client_and_unique_across_clients(monkeypatch):
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)
    first = PydanticAIClient(model="custom@opencodego:m")
    second = PydanticAIClient(model="custom@opencodego:m")
    assert first._opencode_session_id
    assert first._opencode_session_id == first._opencode_session_id
    assert first._opencode_session_id != second._opencode_session_id
    # Same client id → same header across calls.
    url = "https://opencode.ai/zen/go/v1"
    assert (
        opencode_default_headers(url, first._opencode_session_id)[OPENCODE_SESSION_HEADER]
        == opencode_default_headers(url, first._opencode_session_id)[OPENCODE_SESSION_HEADER]
    )
    # Client-level kwargs are stable too.
    assert first._opencode_client_kwargs(url) == first._opencode_client_kwargs(url)


def test_construction_sends_headers_only_for_opencode(monkeypatch):
    import openai

    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.setdefault("calls", []).append(kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.delenv("OPENCODE_SESSION_ID", raising=False)

    async def _build(model, base_url):
        client = PydanticAIClient(model=model, base_url=base_url, api_key="k")
        await client._build_model()

    import asyncio

    asyncio.run(_build("custom@opencodego:glm-5.3-flash", "https://opencode.ai/zen/go/v1"))
    (kwargs,) = captured["calls"]
    assert kwargs["default_headers"][OPENCODE_SESSION_HEADER]
    assert kwargs["default_headers"]["User-Agent"] == OPENCODE_USER_AGENT

    captured["calls"].clear()
    asyncio.run(_build("ollama:llama3.1", "http://localhost:11434/v1"))
    (kwargs,) = captured["calls"]
    assert "default_headers" not in kwargs


def test_env_override_reaches_construction(monkeypatch):
    import openai

    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setenv("OPENCODE_SESSION_ID", "env-sid-123")

    import asyncio

    client = PydanticAIClient(
        model="custom@opencodego:glm-5.3-flash",
        base_url="https://opencode.ai/zen/go/v1",
        api_key="k",
    )
    asyncio.run(client._build_model())
    assert captured["default_headers"][OPENCODE_SESSION_HEADER] == "env-sid-123"
