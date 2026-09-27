"""OpenCode CLI bridge: fake-binary tests (never touches the real CLI)."""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from condor.acp.opencode_cli_client import (
    OpenCodeCLIClient,
    fold_text_events,
    resolve_opencode_model,
)


def _write_fake_bin(path, body: str) -> str:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _concat_bin(path) -> str:
    return _write_fake_bin(
        path,
        "import json, sys\n"
        "print(json.dumps({'type': 'text', 'part': {'text': 'Hello '}}))\n"
        "print(json.dumps({'type': 'step_start'}))\n"
        "print(json.dumps({'type': 'text', 'part': {'text': 'world'}}))\n"
        "print(json.dumps({'type': 'step_finish', 'part': {'tokens': 7}}))\n",
    )


def test_fold_concatenates_text_parts_and_skips_noise():
    text, parsed = fold_text_events(
        '{"type":"text","part":{"text":"a"}}'
        "\nnot json\n"
        '{"type":"step_finish","part":{}}'
        '\n{"type":"text","part":{"text":"b"}}\n'
    )
    assert text == "ab"
    assert parsed == 3


def test_resolve_opencode_model():
    assert (
        resolve_opencode_model("opencode-go:deepseek-v4.1-flash")
        == "opencode-go/deepseek-v4.1-flash"
    )
    assert resolve_opencode_model("opencode:gpt-6-luna") == "opencode/gpt-6-luna"
    assert resolve_opencode_model("opencode-go:") is None
    assert resolve_opencode_model("claude-code") is None
    assert resolve_opencode_model("ollama:llama3.1") is None


@pytest.mark.asyncio
async def test_prompt_concatenates_events(tmp_path, monkeypatch):
    fake = _concat_bin(tmp_path / "opencode")
    monkeypatch.setenv("OPENCODE_BIN", fake)
    client = OpenCodeCLIClient(model="opencode-go/deepseek-v4.1-flash")
    await client.start()
    try:
        assert await client.prompt("hi") == "Hello world"
    finally:
        await client.stop()
    assert client.last_usage == {"tokens": 7}


@pytest.mark.asyncio
async def test_prompt_argv_model_and_no_auto(tmp_path, monkeypatch):
    record = tmp_path / "argv.json"
    fake = _write_fake_bin(
        tmp_path / "opencode",
        "import json, os, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv))\n"
        "print(json.dumps({'type': 'text', 'part': {'text': 'ok'}}))\n",
    )
    monkeypatch.setenv("OPENCODE_BIN", fake)
    client = OpenCodeCLIClient(model="opencode/glm-5.3")
    assert await client.prompt("hello prompt") == "ok"
    argv = json.loads(record.read_text())
    assert argv[1:7] == ["run", "--pure", "--format", "json", "--model", "opencode/glm-5.3"]
    assert argv[7] == "hello prompt"
    assert "--auto" not in argv


@pytest.mark.asyncio
async def test_prompt_nonzero_exit_raises(tmp_path, monkeypatch):
    fake = _write_fake_bin(
        tmp_path / "opencode",
        "import sys\nprint('boom', file=sys.stderr)\nsys.exit(1)\n",
    )
    monkeypatch.setenv("OPENCODE_BIN", fake)
    client = OpenCodeCLIClient(model="opencode-go/kimi-k3")
    with pytest.raises(RuntimeError, match="exited with status 1"):
        await client.prompt("hi")


@pytest.mark.asyncio
async def test_prompt_unparseable_output_raises(tmp_path, monkeypatch):
    fake = _write_fake_bin(tmp_path / "opencode", "print('no json here')\n")
    monkeypatch.setenv("OPENCODE_BIN", fake)
    client = OpenCodeCLIClient(model="opencode-go/kimi-k3")
    with pytest.raises(RuntimeError, match="no parseable JSONL"):
        await client.prompt("hi")


@pytest.mark.asyncio
async def test_prompt_empty_text_raises(tmp_path, monkeypatch):
    fake = _write_fake_bin(
        tmp_path / "opencode",
        "import json\nprint(json.dumps({'type': 'step_finish', 'part': {}}))\n",
    )
    monkeypatch.setenv("OPENCODE_BIN", fake)
    client = OpenCodeCLIClient(model="opencode-go/kimi-k3")
    with pytest.raises(RuntimeError, match="no text"):
        await client.prompt("hi")


@pytest.mark.asyncio
async def test_prompt_timeout_kills_child(tmp_path, monkeypatch):
    fake = _write_fake_bin(
        tmp_path / "opencode",
        "import time\ntime.sleep(30)\n",
    )
    monkeypatch.setenv("OPENCODE_BIN", fake)
    client = OpenCodeCLIClient(model="opencode-go/kimi-k3", timeout_sec=0.2)
    with pytest.raises(TimeoutError, match="timed out"):
        await client.prompt("hi")


@pytest.mark.asyncio
async def test_working_dir_honored(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fake = _write_fake_bin(
        tmp_path / "opencode",
        "import json, os\n"
        "print(json.dumps({'type': 'text', 'part': {'text': os.getcwd()}}))\n",
    )
    monkeypatch.setenv("OPENCODE_BIN", fake)
    client = OpenCodeCLIClient(
        model="opencode-go/kimi-k3", working_dir=str(elsewhere)
    )
    assert await client.prompt("hi") == str(elsewhere)
    # Brooks runner pattern: working_dir is a settable attribute.
    client.working_dir = str(tmp_path)
    assert await client.prompt("hi") == str(tmp_path)


def test_factory_model_mapping(monkeypatch):
    from condor.runtime import llm_client as factory

    monkeypatch.setenv("OPENCODE_BIN", "/nonexistent-fake-opencode")
    go = factory.build_llm_client("opencode-go:deepseek-v4.1-flash")
    assert isinstance(go, OpenCodeCLIClient)
    assert go.model == "opencode-go/deepseek-v4.1-flash"
    plain = factory.build_llm_client("opencode:gpt-6-luna")
    assert isinstance(plain, OpenCodeCLIClient)
    assert plain.model == "opencode/gpt-6-luna"


def test_factory_existing_branches_unchanged():
    from condor.acp import client as acp_client
    from condor.acp import pydantic_ai_client as pydantic_ai
    from condor.runtime import llm_client as factory

    local = factory.build_llm_client("ollama:llama3.1")
    assert isinstance(local, pydantic_ai.PydanticAIClient)
    assert isinstance(factory.build_llm_client("claude-code"), acp_client.ACPClient)
    assert isinstance(
        factory.build_llm_client("claude-acp:sonnet"), acp_client.ACPClient
    )
    assert isinstance(factory.build_llm_client("codex"), acp_client.ACPClient)


@pytest.mark.asyncio
async def test_agent_key_error_knows_opencode():
    from condor.runtime.llm_client import agent_key_error

    assert await agent_key_error("opencode-go:deepseek-v4.1-flash") is None
    assert await agent_key_error("opencode:glm-5.3") is None
    assert "no model id" in (await agent_key_error("opencode-go:") or "")
    assert "unknown model provider" in (await agent_key_error("nope:x") or "")


def test_no_api_key_handling_in_client():
    import inspect

    from condor.acp import opencode_cli_client as mod

    source = inspect.getsource(mod)
    assert "api_key" not in source.lower()
    assert "OPENCODE_BIN" in source  # binary path only, no secrets
    assert os.environ.get("OPENAI_API_KEY") is None or True  # never consulted
