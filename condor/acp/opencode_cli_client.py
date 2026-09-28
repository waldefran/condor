"""OpenCode CLI model bridge -- run prompts through the local ``opencode`` CLI.

V1 limitation: text prompt/response only. This bridge mounts no MCP servers
and performs no native tool calls -- each :meth:`OpenCodeCLIClient.prompt`
spawns one short-lived ``opencode run --pure --format json`` subprocess and
returns the concatenated text parts. That is sufficient for the Brooks roles,
which drive their own JSON tool protocol on top of plain text turns.

Authentication comes from the CLI itself (the user's OpenCode Go
subscription); no API key is used, requested, or stored anywhere here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

log = logging.getLogger(__name__)

#: Agent-key prefixes routed to this bridge (``opencode-go:<model>``).
OPENCODE_PREFIXES = frozenset({"opencode-go", "opencode"})

#: Env var overriding the ``opencode`` binary path.
OPENCODE_BIN_ENV = "OPENCODE_BIN"

#: Env var overriding the per-prompt timeout in seconds.
OPENCODE_TIMEOUT_ENV = "OPENCODE_TIMEOUT_SEC"

#: Default per-prompt timeout in seconds.
DEFAULT_TIMEOUT_SEC = 600.0


def resolve_opencode_model(agent_key: str) -> str | None:
    """Map an agent key to a CLI ``--model`` id, or ``None`` when not ours.

    ``"opencode-go:deepseek-v4.1-flash"`` → ``"opencode-go/deepseek-v4.1-flash"``.
    A bare prefix with no model id (``"opencode-go:"``) is not routable.
    """
    base, sep, name = agent_key.partition(":")
    if not sep or base not in OPENCODE_PREFIXES:
        return None
    name = name.strip()
    if not name:
        return None
    return f"{base}/{name}"


def _extract_text(event: dict) -> str | None:
    """Text payload of one ``opencode run --format json`` JSONL event."""
    if not isinstance(event, dict) or event.get("type") != "text":
        return None
    part = event.get("part")
    if isinstance(part, dict) and isinstance(part.get("text"), str):
        return part["text"]
    text = event.get("text")
    return text if isinstance(text, str) else None


def fold_text_events(stdout: str) -> tuple[str, int]:
    """Concatenate text parts from JSONL ``stdout``.

    Returns ``(text, parsed_events)`` where ``parsed_events`` counts lines
    that decoded as JSON. Non-JSON lines are ignored; callers decide whether
    zero parsed events means unparseable output.
    """
    chunks: list[str] = []
    parsed = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        parsed += 1
        text = _extract_text(event)
        if text:
            chunks.append(text)
    return "".join(chunks), parsed


class OpenCodeCLIClient:
    """One subprocess per prompt against the authenticated ``opencode`` CLI.

    Exposes the surface the existing surfaces rely on: ``start()``,
    ``prompt(text) -> str``, ``stop()``, a settable ``working_dir``, and a
    ``model`` attribute holding the full CLI model id (``provider/model``).

    The CLI has no system-instruction channel and each prompt is a fresh
    subprocess, so ``build_llm_client`` callers must inline any system prompt
    and resend the transcript for multi-turn loops. The capability flags below
    let them detect that instead of silently losing the instructions.
    """

    accepts_system_prompt = False
    keeps_history = False

    def __init__(
        self,
        model: str,
        working_dir: str | None = None,
        timeout_sec: float | None = None,
        opencode_bin: str | None = None,
    ):
        if not model or "/" not in model:
            raise ValueError(
                f"OpenCodeCLIClient needs a CLI model id like "
                f"'opencode-go/<model>', got {model!r}"
            )
        self.model = model
        self.working_dir = working_dir or os.getcwd()
        self.timeout_sec = timeout_sec
        self.opencode_bin = opencode_bin
        # Last step_finish payload seen (tokens/cost), when the CLI emitted one.
        self.last_usage: dict | None = None

    def _bin(self) -> str:
        return self.opencode_bin or os.environ.get(OPENCODE_BIN_ENV, "opencode")

    def _timeout(self) -> float:
        if self.timeout_sec is not None:
            return self.timeout_sec
        try:
            return float(os.environ.get(OPENCODE_TIMEOUT_ENV, DEFAULT_TIMEOUT_SEC))
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT_SEC

    async def start(self) -> None:
        """No persistent session -- the subprocess is per-prompt. No-op."""

    async def stop(self) -> None:
        """No persistent session -- nothing to tear down. No-op."""

    def _argv(self, text: str) -> list[str]:
        # Never --auto: the bridge must not act beyond answering the prompt.
        return [
            self._bin(),
            "run",
            "--pure",
            "--format",
            "json",
            "--model",
            self.model,
            text,
        ]

    async def prompt(self, text: str) -> str:
        """Run one subprocess for ``text`` and return the concatenated reply."""
        argv = self._argv(text)
        timeout = self._timeout()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir or None,
            )
        except OSError as exc:
            raise RuntimeError(
                f"opencode CLI could not be launched ({argv[0]!r}): {exc}"
            ) from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.communicate()
            except Exception:  # noqa: BLE001 - already failing; just reap
                pass
            raise TimeoutError(
                f"opencode CLI prompt timed out after {timeout}s and was killed "
                f"(model={self.model})"
            ) from None
        stdout = out.decode(errors="replace")
        stderr = err.decode(errors="replace").strip()
        if proc.returncode != 0:
            detail = f"\nCLI stderr:\n{stderr}" if stderr else ""
            raise RuntimeError(
                f"opencode CLI exited with status {proc.returncode} "
                f"(model={self.model}).{detail}"
            )
        answer, parsed = fold_text_events(stdout)
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict) and event.get("type") == "step_finish":
                part = event.get("part")
                if isinstance(part, dict):
                    self.last_usage = part
        if parsed == 0:
            tail = stdout.strip()[-500:] if stdout.strip() else "<empty stdout>"
            raise RuntimeError(
                f"opencode CLI produced no parseable JSONL output "
                f"(model={self.model}): {tail}"
            )
        if not answer.strip():
            raise RuntimeError(
                f"opencode CLI returned no text (model={self.model}, "
                f"{parsed} JSONL event(s) parsed)"
            )
        return answer
