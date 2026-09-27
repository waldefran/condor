"""Durable Brooks JSONL audit trail and shared state under a strategy home."""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Mapping

from condor.fsutil import atomic_write_json

from .events import BrooksEvent

_TRADE_DOCUMENTS = frozenset(
    {
        "binding.json",
        "original_trade_intent.json",
        "latest_management_intent.json",
        "hedge_state.json",
    }
)
_TRADE_HISTORIES = frozenset({"management_history.jsonl", "executions.jsonl"})


def _json_line(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(value), separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class BrooksStore:
    """One strategy's Brooks state; each completed append is fsynced."""

    def __init__(self, strategy_home: Path):
        self.root = Path(strategy_home) / "brooks_state"
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("trader", "htf", "trades"):
            (self.root / name).mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self.events_path = self.root / "events.jsonl"
        self.events_path.touch(exist_ok=True)
        _fsync_dir(self.root)
        self._recover_latest("trader")
        self._recover_latest("htf")

    def _append(self, path: Path, value: Mapping[str, Any]) -> None:
        line = _json_line(value)  # Refuse non-JSON before touching the file.
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                size = os.lseek(fd, 0, os.SEEK_END)
                if size:
                    os.lseek(fd, -1, os.SEEK_END)
                    if os.read(fd, 1) != b"\n":
                        # A process crash can leave one incomplete final record.
                        os.lseek(fd, 0, os.SEEK_SET)
                        tail = os.read(fd, size)
                        os.ftruncate(fd, tail.rfind(b"\n") + 1)
                written = 0
                while written < len(line):
                    written += os.write(fd, line[written:])
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_dir(path.parent)

    @staticmethod
    def read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            for line in handle:
                if not line.endswith(b"\n"):
                    break  # incomplete tail; next append removes it
                rows.append(json.loads(line))
        return rows

    def append_event(self, event: BrooksEvent) -> None:
        self._append(self.events_path, event.to_dict())

    def read_events(self) -> list[BrooksEvent]:
        return [BrooksEvent.from_dict(row) for row in self.read_jsonl(self.events_path)]

    def _save_latest(self, role: str, value: Mapping[str, Any]) -> None:
        directory = self.root / role
        with self._lock:
            self._append(directory / "history.jsonl", value)
            atomic_write_json(directory / "latest.json", dict(value), allow_nan=False)

    def _recover_latest(self, role: str) -> None:
        directory = self.root / role
        history = self.read_jsonl(directory / "history.jsonl")
        if history:
            latest = directory / "latest.json"
            try:
                current = json.loads(latest.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                current = None
            if current != history[-1]:
                atomic_write_json(latest, history[-1], allow_nan=False)

    def save_trader_intent(self, value: Mapping[str, Any]) -> None:
        self._save_latest("trader", value)

    def save_market_context(self, value: Mapping[str, Any]) -> None:
        self._save_latest("htf", value)

    def read_latest(self, role: str) -> dict[str, Any] | None:
        if role not in {"trader", "htf"}:
            raise ValueError("role must be trader or htf")
        path = self.root / role / "latest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _trade_dir(self, correlation_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", correlation_id):
            raise ValueError("invalid correlation_id")
        return self.root / "trades" / correlation_id

    def write_trade_document(
        self, correlation_id: str, name: str, value: Mapping[str, Any]
    ) -> None:
        if name not in _TRADE_DOCUMENTS:
            raise ValueError("unsupported trade document")
        with self._lock:
            atomic_write_json(
                self._trade_dir(correlation_id) / name, dict(value), allow_nan=False
            )

    def read_trade_document(
        self, correlation_id: str, name: str
    ) -> dict[str, Any] | None:
        if name not in _TRADE_DOCUMENTS:
            raise ValueError("unsupported trade document")
        path = self._trade_dir(correlation_id) / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def append_trade_history(
        self, correlation_id: str, name: str, value: Mapping[str, Any]
    ) -> None:
        if name not in _TRADE_HISTORIES:
            raise ValueError("unsupported trade history")
        self._append(self._trade_dir(correlation_id) / name, value)

    def flush(self) -> None:
        """Writes are fsynced individually; retain an explicit stop boundary."""
        _fsync_dir(self.root)
