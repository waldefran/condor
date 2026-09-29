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

from .contracts import MarketContextV1, MarketContextV2
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
        self.strategy_home = Path(strategy_home)
        self.root = self.strategy_home / "brooks_state"
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("trader", "htf", "trades", "context/d1", "context/h4"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.events_path = self.root / "events.jsonl"
        self.events_path.touch(exist_ok=True)
        _fsync_dir(self.root)
        self._recover_latest("trader")
        self._recover_latest("htf")
        self._recover_context_latest("d1")
        self._recover_context_latest("h4")

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

    def save_market_context(
        self, value: Mapping[str, Any] | MarketContextV1 | MarketContextV2
    ) -> None:
        raw = (
            value.model_dump(mode="json")
            if isinstance(value, (MarketContextV1, MarketContextV2))
            else dict(value)
        )
        if raw.get("schema") == "brooks.market-context.v2":
            context = MarketContextV2.model_validate(raw)
            self._save_context_latest(context.timeframe.lower(), context.model_dump(mode="json"))
            return
        # Preserve the V1 path and bytes for old snapshots. Readers below expose
        # a conservative V2 view when a valid D1 V1 context is all that exists.
        self._save_latest("htf", raw)

    def _save_context_latest(self, timeframe: str, value: Mapping[str, Any]) -> None:
        if timeframe not in {"d1", "h4"}:
            raise ValueError("context timeframe must be D1 or H4")
        directory = self.root / "context" / timeframe
        with self._lock:
            self._append(directory / "history.jsonl", value)
            atomic_write_json(directory / "latest.json", dict(value), allow_nan=False)

    def _recover_context_latest(self, timeframe: str) -> None:
        directory = self.root / "context" / timeframe
        history = self.read_jsonl(directory / "history.jsonl")
        if not history:
            return
        latest = directory / "latest.json"
        try:
            current = json.loads(latest.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            current = None
        if current != history[-1]:
            atomic_write_json(latest, history[-1], allow_nan=False)

    @staticmethod
    def _canonical_context_timeframe(timeframe: str) -> tuple[str, str]:
        if not isinstance(timeframe, str):
            raise ValueError("context timeframe must be D1 or H4")
        canonical = {"1d": "D1", "d1": "D1", "4h": "H4", "h4": "H4"}.get(
            timeframe.strip().lower()
        )
        if canonical is None:
            raise ValueError("context timeframe must be D1 or H4")
        return canonical, canonical.lower()

    @staticmethod
    def _legacy_context_v2(raw: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return a clearly low-confidence structural view of a V1 D1 record."""
        try:
            legacy = MarketContextV1.model_validate(dict(raw))
            if legacy.timeframe.strip().lower() not in {"d1", "1d"}:
                return None
            uncertainty = list(legacy.uncertainty)
            return MarketContextV2.model_validate(
                {
                    "schema": "brooks.market-context.v2",
                    "role": "CONTEXT_ANALYST",
                    "symbol": legacy.symbol,
                    "timeframe": "D1",
                    "decision_time_ms": legacy.decision_time_ms,
                    "window_bars": 0,
                    "primary_regime": "transition-unclear",
                    "phase": "unclear",
                    "breakout_mode": "unclear",
                    "directional_pressure": "unclear",
                    "always_in": "unclear",
                    "always_in_relevance": "low",
                    "observations": legacy.observations,
                    "structures": [],
                    "evidence_for": [
                        "Legacy context did not record structured supporting evidence."
                    ],
                    "evidence_against": legacy.evidence_against,
                    "transition_conditions": [
                        "Legacy context did not record transition conditions."
                    ],
                    "missing_information": [
                        *uncertainty,
                        "Legacy context did not record regime axes or source window size.",
                    ],
                    "confidence": "low",
                }
            ).model_dump(mode="json")
        except Exception:
            return None

    def read_market_context(
        self, timeframe: str, *, symbol: str | None = None
    ) -> dict[str, Any] | None:
        """Read the latest typed context for D1/H4, with a D1 V1 fallback.

        If a strategy tracks multiple symbols, history is searched backward for
        that symbol when the timeframe's global latest belongs to another one.
        """
        canonical, directory_name = self._canonical_context_timeframe(timeframe)
        directory = self.root / "context" / directory_name
        candidates: list[Mapping[str, Any]] = []
        latest = directory / "latest.json"
        if latest.exists():
            try:
                parsed = json.loads(latest.read_text(encoding="utf-8"))
                if isinstance(parsed, Mapping):
                    candidates.append(parsed)
            except (OSError, json.JSONDecodeError):
                pass
        history = self.read_jsonl(directory / "history.jsonl")
        candidates.extend(reversed(history))
        for candidate in candidates:
            try:
                context = MarketContextV2.model_validate(dict(candidate))
            except Exception:
                continue
            if context.timeframe != canonical or (symbol and context.symbol != symbol):
                continue
            return context.model_dump(mode="json")

        if canonical == "D1":
            legacy_path = self.root / "htf" / "latest.json"
            try:
                legacy_raw = json.loads(legacy_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                legacy_raw = None
            if isinstance(legacy_raw, Mapping):
                migrated_view = self._legacy_context_v2(legacy_raw)
                if migrated_view and (symbol is None or migrated_view["symbol"] == symbol):
                    return migrated_view
        return None

    def read_market_contexts(
        self,
        timeframes: tuple[str, ...] | list[str] = ("D1", "H4"),
        *,
        symbol: str | None = None,
    ) -> dict[str, dict[str, Any] | None]:
        contexts: dict[str, dict[str, Any] | None] = {}
        for timeframe in timeframes:
            canonical, _ = self._canonical_context_timeframe(timeframe)
            contexts[canonical] = self.read_market_context(canonical, symbol=symbol)
        return contexts

    def read_latest(self, role: str) -> dict[str, Any] | None:
        if role not in {"trader", "htf"}:
            raise ValueError("role must be trader or htf")
        path = self.root / role / "latest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _trade_dir(self, correlation_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", correlation_id):
            raise ValueError("invalid correlation_id")
        gm_trades = self.strategy_home / "trades" / correlation_id
        if gm_trades.exists() or (self.strategy_home / "trades").exists():
            return gm_trades
        return self.root / "trades" / correlation_id

    def write_trade_document(
        self, correlation_id: str, name: str, value: Mapping[str, Any]
    ) -> None:
        if name not in _TRADE_DOCUMENTS:
            raise ValueError("unsupported trade document")
        target_dir = self._trade_dir(correlation_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            atomic_write_json(
                target_dir / name, dict(value), allow_nan=False
            )

    def read_trade_document(
        self, correlation_id: str, name: str
    ) -> dict[str, Any] | None:
        if name not in _TRADE_DOCUMENTS:
            raise ValueError("unsupported trade document")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", correlation_id):
            raise ValueError("invalid correlation_id")
        for parent in (self.strategy_home / "trades", self.root / "trades"):
            path = parent / correlation_id / name
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        return None

    def append_trade_history(
        self, correlation_id: str, name: str, value: Mapping[str, Any]
    ) -> None:
        if name not in _TRADE_HISTORIES:
            raise ValueError("unsupported trade history")
        self._append(self._trade_dir(correlation_id) / name, value)

    def flush(self) -> None:
        """Writes are fsynced individually; retain an explicit stop boundary."""
        _fsync_dir(self.root)
