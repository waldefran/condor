"""Durable idempotency records for frozen Brooks Trader decision rounds."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from condor.fsutil import atomic_write_json


def decision_cycle_id(symbol: str, decision_time_ms: int, role: str = "TRADER") -> str:
    identity = json.dumps(
        [symbol, decision_time_ms, role], separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


class DecisionCycleStore:
    """Persist the latest state per (symbol, decision time, role).

    The frozen input lives in one atomic per-cycle document. A compact JSONL
    history records each lifecycle transition without duplicating candle data.
    """

    def __init__(self, brooks_root: Path):
        self.root = Path(brooks_root) / "trader" / "cycles"
        self.root.mkdir(parents=True, exist_ok=True)
        self.history_path = self.root.parent / "decision_cycles.jsonl"
        self.lock_path = self.root.parent / "decision_cycles.lock"
        self.history_path.touch(exist_ok=True)

    @staticmethod
    def identity(symbol: str, decision_time_ms: int, role: str = "TRADER") -> str:
        if not symbol or not role:
            raise ValueError("decision cycle identity requires symbol and role")
        if (
            isinstance(decision_time_ms, bool)
            or not isinstance(decision_time_ms, int)
            or decision_time_ms < 0
        ):
            raise ValueError("decision cycle time must be a nonnegative integer")
        return decision_cycle_id(symbol, decision_time_ms, role)

    def _path(self, cycle_id: str) -> Path:
        if len(cycle_id) != 64 or any(c not in "0123456789abcdef" for c in cycle_id):
            raise ValueError("invalid decision cycle id")
        return self.root / f"{cycle_id}.json"

    def get(self, cycle_id: str) -> dict[str, Any] | None:
        path = self._path(cycle_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict):
            raise ValueError("decision cycle record is not an object")
        return value

    def _append_history(self, row: Mapping[str, Any]) -> None:
        """Append while caller holds ``decision_cycles.lock`` exclusively."""
        payload = (json.dumps(dict(row), separators=(",", ":"), allow_nan=False) + "\n").encode()
        fd = os.open(self.history_path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            size = os.lseek(fd, 0, os.SEEK_END)
            if size:
                os.lseek(fd, -1, os.SEEK_END)
                if os.read(fd, 1) != b"\n":
                    os.lseek(fd, 0, os.SEEK_SET)
                    tail = os.read(fd, size)
                    os.ftruncate(fd, tail.rfind(b"\n") + 1)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def create_pending(
        self, *, symbol: str, decision_time_ms: int, role: str = "TRADER", created_at_ms: int
    ) -> tuple[str, dict[str, Any]]:
        cycle_id = self.identity(symbol, decision_time_ms, role)
        path = self._path(cycle_id)
        lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if path.exists():
                current = json.loads(path.read_text(encoding="utf-8"))
                return cycle_id, current
            current = {
                "schema": "brooks.decision-cycle.v1",
                "cycle_id": cycle_id,
                "symbol": symbol,
                "decision_time_ms": decision_time_ms,
                "role": role,
                "status": "pending",
                "attempt": 0,
                "created_at_ms": created_at_ms,
                "updated_at_ms": created_at_ms,
                "packet_hash": None,
                "frozen_packet": None,
                "intent": None,
                "event_id": None,
                "failure": None,
            }
            atomic_write_json(path, current, allow_nan=False)
            self._append_history(
                {
                    "cycle_id": cycle_id,
                    "symbol": symbol,
                    "decision_time_ms": decision_time_ms,
                    "role": role,
                    "status": "pending",
                    "attempt": 0,
                    "updated_at_ms": created_at_ms,
                }
            )
            return cycle_id, current
        finally:
            os.close(lock_fd)

    def update(
        self,
        cycle_id: str,
        *,
        status: str,
        updated_at_ms: int,
        **fields: Any,
    ) -> dict[str, Any]:
        if status not in {"pending", "running", "retrying", "completed", "failed"}:
            raise ValueError("unsupported decision cycle status")
        path = self._path(cycle_id)
        lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            current = json.loads(path.read_text(encoding="utf-8"))
            previous = current.get("status")
            transitions = {
                "pending": {"pending", "running", "failed"},
                "running": {"running", "retrying", "completed", "failed"},
                "retrying": {"retrying", "completed", "failed"},
                "completed": {"completed"},
                "failed": {"failed"},
            }
            if status not in transitions.get(previous, set()):
                raise ValueError(f"invalid decision cycle transition {previous} -> {status}")
            current.update(fields)
            current["status"] = status
            current["updated_at_ms"] = updated_at_ms
            atomic_write_json(path, current, allow_nan=False)
            self._append_history(
                {
                    "cycle_id": cycle_id,
                    "symbol": current["symbol"],
                    "decision_time_ms": current["decision_time_ms"],
                    "role": current["role"],
                    "status": status,
                    "attempt": current.get("attempt", 0),
                    "packet_hash": current.get("packet_hash"),
                    "failure": current.get("failure"),
                    "tool_audit": fields.get("tool_audit", current.get("tool_audit", [])),
                    "updated_at_ms": updated_at_ms,
                }
            )
            return current
        finally:
            os.close(lock_fd)


class MemoryDecisionCycleStore:
    """Test adapter for injected stores that have no filesystem root."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def create_pending(
        self, *, symbol: str, decision_time_ms: int, role: str = "TRADER", created_at_ms: int
    ) -> tuple[str, dict[str, Any]]:
        cycle_id = DecisionCycleStore.identity(symbol, decision_time_ms, role)
        record = self.records.get(cycle_id)
        if record is None:
            record = {
                "schema": "brooks.decision-cycle.v1",
                "cycle_id": cycle_id,
                "symbol": symbol,
                "decision_time_ms": decision_time_ms,
                "role": role,
                "status": "pending",
                "attempt": 0,
                "created_at_ms": created_at_ms,
                "updated_at_ms": created_at_ms,
                "packet_hash": None,
                "frozen_packet": None,
                "intent": None,
                "event_id": None,
                "failure": None,
            }
            self.records[cycle_id] = record
        return cycle_id, dict(record)

    def get(self, cycle_id: str) -> dict[str, Any] | None:
        record = self.records.get(cycle_id)
        return dict(record) if record is not None else None

    def update(
        self,
        cycle_id: str,
        *,
        status: str,
        updated_at_ms: int,
        **fields: Any,
    ) -> dict[str, Any]:
        record = self.records[cycle_id]
        previous = record["status"]
        transitions = {
            "pending": {"pending", "running", "failed"},
            "running": {"running", "retrying", "completed", "failed"},
            "retrying": {"retrying", "completed", "failed"},
            "completed": {"completed"},
            "failed": {"failed"},
        }
        if status not in transitions.get(previous, set()):
            raise ValueError(f"invalid decision cycle transition {previous} -> {status}")
        record.update(fields)
        record["status"] = status
        record["updated_at_ms"] = updated_at_ms
        return dict(record)
