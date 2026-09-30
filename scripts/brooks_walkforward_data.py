#!/usr/bin/env python3
"""Fetch and freeze real Hummingbot candles for a bounded Brooks walk-forward.

The loader reads only ``POST /market-data/historical-candles`` through the
configured Hummingbot API client. It never requests account state or submits
orders. Pages, canonical candle files, and a SHA-256 manifest are stored in
``/tmp/brooks-walkforward-10d-data`` by default.

The public :class:`FrozenHistoricalSource` is a cache-backed, time-bounded
source compatible with Brooks TRADER and PM candle readers. Its only candle
read method is ``fetch_candles(symbol, timeframe, limit)``; ``get_candles``
and ``getCandles`` are PM/client aliases. Every returned bar is closed at or
before that source view's decision time.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from condor.fetchers.market_data import fetch_historical_candles


SYMBOL = "ETH-USDT"
CONNECTOR = "binance_perpetual_demo"
EVALUATION_START = "2026-09-20T00:00:00Z"
EVALUATION_END = "2026-09-30T00:00:00Z"  # exclusive
DEFAULT_OUTPUT = Path("/tmp/brooks-walkforward-10d-data")
PAGE_BARS = 1_000
DEFAULT_WARMUP_BARS = 122
DEFAULT_ONE_MINUTE_WARMUP_BARS = 130
SCHEMA_VERSION = "brooks.walkforward.candles.v1"
ENDPOINT = "POST /market-data/historical-candles"

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
TIMEFRAME_ALIASES: dict[str, str] = {
    "M1": "1m",
    "M15": "15m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
}
TIMEFRAMES = ("1m", "15m", "1h", "4h", "1d")


class DataAvailabilityError(RuntimeError):
    """The source returned a gap or incomplete history for a requested window."""


def _utc_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("date must include a UTC offset")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _iso_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_timeframe(timeframe: str) -> str:
    if not isinstance(timeframe, str):
        raise ValueError("timeframe must be a string")
    value = TIMEFRAME_ALIASES.get(timeframe.upper(), timeframe.lower())
    if value not in INTERVAL_MS:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    return value


def _row_timestamp_ms(row: Mapping[str, Any]) -> int:
    for key in ("timestamp", "open_time_ms", "open_time", "ts", "time"):
        raw = row.get(key)
        if raw is not None:
            if isinstance(raw, bool):
                raise ValueError("boolean candle timestamp")
            stamp = float(raw)
            if not math.isfinite(stamp) or stamp < 0:
                raise ValueError("invalid candle timestamp")
            return int(stamp * 1000) if stamp < 1e12 else int(stamp)
    raise ValueError("candle row has no timestamp")


def _decimal_string(value: Any, *, nonnegative: bool = False) -> str:
    from decimal import Decimal, InvalidOperation

    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid candle numeric value") from exc
    if not number.is_finite() or (nonnegative and number < 0):
        raise ValueError("invalid candle numeric value")
    return format(number.normalize(), "f")


def _normalize_bar(row: Mapping[str, Any], timeframe: str) -> dict[str, Any]:
    opened = _row_timestamp_ms(row)
    interval = INTERVAL_MS[timeframe]
    if opened % interval:
        raise ValueError(f"unaligned {timeframe} candle timestamp: {opened}")
    bar: dict[str, Any] = {
        "open_time_ms": opened,
        "close_time_ms": opened + interval - 1,
        "open": _decimal_string(row["open"]),
        "high": _decimal_string(row["high"]),
        "low": _decimal_string(row["low"]),
        "close": _decimal_string(row["close"]),
        "closed": True,
    }
    volume = row.get("volume")
    if volume is not None:
        bar["volume"] = _decimal_string(volume, nonnegative=True)
    return bar


def _unwrap_candle_file(path: Path) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected candle object")
            bars.append(value)
    return bars


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
    os.replace(temporary, path)


def _validate_decision_time(decision_ms: int) -> int:
    if isinstance(decision_ms, bool) or not isinstance(decision_ms, int) or decision_ms < 0:
        raise ValueError("decision_ms must be a nonnegative integer")
    return decision_ms


class FrozenHistoricalSource:
    """Read-only local candle source with a hard inclusive close-time ceiling.

    Construct with the raw dataset ceiling and then use ``at_decision_time``
    for each simulated decision. Successive views can only move the ceiling
    earlier, so no provider or cached future row can leak into a model input.
    """

    def __init__(
        self,
        bars_by_timeframe: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        symbol: str = SYMBOL,
        decision_ms: int,
    ) -> None:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        self._symbol = symbol
        self._decision_ms = _validate_decision_time(decision_ms)
        self._bars: dict[str, tuple[dict[str, Any], ...]] = {}
        for raw_timeframe, rows in bars_by_timeframe.items():
            timeframe = _canonical_timeframe(raw_timeframe)
            interval = INTERVAL_MS[timeframe]
            normalized: list[dict[str, Any]] = []
            seen: set[int] = set()
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError("cached candle must be an object")
                opened = int(row["open_time_ms"])
                closed = int(row["close_time_ms"])
                if closed != opened + interval - 1 or opened % interval:
                    raise ValueError(f"invalid {timeframe} cached close timestamp")
                if opened in seen:
                    raise ValueError(f"duplicate {timeframe} cached candle")
                seen.add(opened)
                if row.get("closed") is not True:
                    raise ValueError("cached candle is not marked closed")
                bar = {
                    "open_time_ms": opened,
                    "close_time_ms": closed,
                    "open": _decimal_string(row["open"]),
                    "high": _decimal_string(row["high"]),
                    "low": _decimal_string(row["low"]),
                    "close": _decimal_string(row["close"]),
                    "closed": True,
                }
                if row.get("volume") is not None:
                    bar["volume"] = _decimal_string(row["volume"], nonnegative=True)
                normalized.append(bar)
            normalized.sort(key=lambda bar: bar["open_time_ms"])
            self._bars[timeframe] = tuple(normalized)

    @classmethod
    def from_directory(
        cls,
        directory: str | Path = DEFAULT_OUTPUT,
        *,
        symbol: str = SYMBOL,
        decision_ms: int | None = None,
    ) -> "FrozenHistoricalSource":
        root = Path(directory)
        bars: dict[str, list[dict[str, Any]]] = {}
        for timeframe in TIMEFRAMES:
            path = root / f"{symbol.replace('-', '_')}_{timeframe}.jsonl"
            if path.is_file():
                bars[timeframe] = _unwrap_candle_file(path)
        if decision_ms is None:
            decision_ms = _utc_ms(EVALUATION_END) - 1
        return cls(bars, symbol=symbol, decision_ms=decision_ms)

    @property
    def decision_ms(self) -> int:
        return self._decision_ms

    def at_decision_time(self, decision_ms: int) -> "FrozenHistoricalSource":
        ceiling = min(self._decision_ms, _validate_decision_time(decision_ms))
        return FrozenHistoricalSource(
            self._bars,
            symbol=self._symbol,
            decision_ms=ceiling,
        )

    async def fetch_candles(
        self, symbol: str, timeframe: str, limit: int
    ) -> list[dict[str, Any]]:
        if symbol != self._symbol:
            raise ValueError("symbol does not match frozen dataset")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        canonical = _canonical_timeframe(timeframe)
        rows = self._bars.get(canonical, ())
        eligible = [bar for bar in rows if bar["close_time_ms"] <= self._decision_ms]
        # Return detached dictionaries; caller edits cannot change the cache.
        return deepcopy(list(eligible[-limit:]))

    async def get_candles(
        self, symbol: str, timeframe: str, limit: int
    ) -> list[dict[str, Any]]:
        """PM snake-case alias for ``fetch_candles``."""
        return await self.fetch_candles(symbol, timeframe, limit)

    async def getCandles(
        self, symbol: str, timeframe: str, limit: int
    ) -> list[dict[str, Any]]:
        """PM legacy camel-case alias for ``fetch_candles``."""
        return await self.fetch_candles(symbol, timeframe, limit)


class HistoricalDataLoader:
    """Paginated, resumable historical candle fetcher and manifest writer."""

    def __init__(
        self,
        client: Any,
        *,
        output_dir: Path,
        symbol: str = SYMBOL,
        connector: str = CONNECTOR,
        start_ms: int,
        end_ms: int,
        warmup_bars: int = DEFAULT_WARMUP_BARS,
        one_minute_warmup_bars: int = DEFAULT_ONE_MINUTE_WARMUP_BARS,
        page_bars: int = PAGE_BARS,
    ) -> None:
        if end_ms <= start_ms:
            raise ValueError("end_ms must be after start_ms")
        if warmup_bars < 120:
            raise ValueError("at least 120 warmup bars are required")
        if one_minute_warmup_bars < 120:
            raise ValueError("at least 120 one-minute warmup bars are required")
        if page_bars < 1:
            raise ValueError("page_bars must be positive")
        self.client = client
        self.output_dir = output_dir
        self.symbol = symbol
        self.connector = connector
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.warmup_bars = warmup_bars
        self.one_minute_warmup_bars = one_minute_warmup_bars
        self.page_bars = page_bars
        self.page_cache_dir = output_dir / "pages"

    def _cache_path(self, timeframe: str, page_start_ms: int, page_end_ms: int) -> Path:
        safe_symbol = self.symbol.replace("/", "_").replace("-", "_")
        return self.page_cache_dir / (
            f"{safe_symbol}_{timeframe}_{page_start_ms}_{page_end_ms}.json"
        )

    def _load_cached_page(
        self, path: Path, timeframe: str, page_start_ms: int, page_end_ms: int
    ) -> list[dict[str, Any]] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("schema_version") != SCHEMA_VERSION
                or payload.get("connector") != self.connector
                or payload.get("symbol") != self.symbol
                or payload.get("timeframe") != timeframe
                or payload.get("request_start_ms") != page_start_ms
                or payload.get("request_end_exclusive_ms") != page_end_ms
                or not isinstance(payload.get("bars"), list)
            ):
                return None
            rows = payload["bars"]
            for row in rows:
                if not page_start_ms <= int(row["open_time_ms"]) < page_end_ms:
                    return None
            return rows
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    async def _fetch_page(
        self, timeframe: str, page_start_ms: int, page_end_ms: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        path = self._cache_path(timeframe, page_start_ms, page_end_ms)
        cached = self._load_cached_page(path, timeframe, page_start_ms, page_end_ms)
        if cached is not None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return cached, {
                "cache": str(path),
                "cache_sha256": _sha256(path),
                "retrieved_at_utc": payload.get("retrieved_at_utc"),
                "status": "cached",
                "count": len(cached),
            }

        # Hummingbot accepts seconds and has an inclusive end filter in some
        # connector versions. Ending one second before the next page boundary
        # prevents the next page's first open from being requested twice.
        request_start_sec = page_start_ms // 1000
        request_end_sec = (page_end_ms - 1) // 1000
        raw_rows = await fetch_historical_candles(
            self.client,
            self.connector,
            self.symbol,
            timeframe,
            start_time=request_start_sec,
            end_time=request_end_sec,
            fallback_on_error=False,
            strict=True,
        )
        bars: dict[int, dict[str, Any]] = {}
        for row in raw_rows:
            opened = _row_timestamp_ms(row)
            if not page_start_ms <= opened < page_end_ms:
                continue
            bar = _normalize_bar(row, timeframe)
            if bar["close_time_ms"] >= self.end_ms:
                continue
            if bar["close_time_ms"] > int(time.time_ns() // 1_000_000):
                # The source itself must return closed data; do not cache a
                # forming candle even if an endpoint includes its open time.
                continue
            if opened in bars and bars[opened] != bar:
                raise ValueError(f"conflicting duplicate candle at {opened}")
            bars[opened] = bar

        result = [bars[key] for key in sorted(bars)]
        _write_json(
            path,
            {
                "schema_version": SCHEMA_VERSION,
                "source_endpoint": ENDPOINT,
                "connector": self.connector,
                "symbol": self.symbol,
                "timeframe": timeframe,
                "request_start_ms": page_start_ms,
                "request_end_exclusive_ms": page_end_ms,
                "request_start_time_s": request_start_sec,
                "request_end_time_s": request_end_sec,
                "retrieved_at_utc": _iso_ms(time.time_ns() // 1_000_000),
                "bars": result,
            },
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        return result, {
            "cache": str(path),
            "cache_sha256": _sha256(path),
            "retrieved_at_utc": payload["retrieved_at_utc"],
            "status": "fetched",
            "count": len(result),
        }

    async def fetch_timeframe(self, timeframe: str) -> dict[str, Any]:
        interval = INTERVAL_MS[timeframe]
        warmup_bars = (
            self.one_minute_warmup_bars if timeframe == "1m" else self.warmup_bars
        )
        warmup_start_ms = self.start_ms - warmup_bars * interval
        if warmup_start_ms % interval or self.start_ms % interval or self.end_ms % interval:
            raise ValueError(f"requested {timeframe} boundaries are not interval aligned")
        expected_count = (self.end_ms - warmup_start_ms) // interval
        combined: dict[int, dict[str, Any]] = {}
        pages: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        cursor = warmup_start_ms
        while cursor < self.end_ms:
            page_end = min(self.end_ms, cursor + self.page_bars * interval)
            try:
                rows, page_info = await self._fetch_page(timeframe, cursor, page_end)
                for bar in rows:
                    opened = int(bar["open_time_ms"])
                    if opened in combined and combined[opened] != bar:
                        raise ValueError(f"conflicting duplicate candle at {opened}")
                    combined[opened] = bar
                page_info.update(
                    {
                        "start_utc": _iso_ms(cursor),
                        "end_exclusive_utc": _iso_ms(page_end),
                    }
                )
                pages.append(page_info)
            except Exception as exc:  # preserve all other intervals/pages on source failure
                errors.append(
                    {
                        "start_utc": _iso_ms(cursor),
                        "end_exclusive_utc": _iso_ms(page_end),
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                )
            cursor = page_end

        bars = [combined[key] for key in sorted(combined)]
        expected_opens = range(warmup_start_ms, self.end_ms, interval)
        actual_opens = set(combined)
        missing = [stamp for stamp in expected_opens if stamp not in actual_opens]
        unexpected = [stamp for stamp in actual_opens if not warmup_start_ms <= stamp < self.end_ms]
        contiguous = not missing and not unexpected and not errors and len(bars) == expected_count

        # Persist partial real data as well as complete data. Missing bars are
        # listed in the manifest and never interpolated or synthesized.
        data_path = self.output_dir / f"{self.symbol.replace('-', '_')}_{timeframe}.jsonl"
        _write_jsonl(data_path, bars)
        evaluation = [bar for bar in bars if self.start_ms <= bar["open_time_ms"] < self.end_ms]
        warmup = [bar for bar in bars if bar["open_time_ms"] < self.start_ms]
        if timeframe == "1m":
            bootstrap_decision_ms = self.start_ms - 10 * INTERVAL_MS["1m"]
        elif timeframe in {"4h", "1d"}:
            bootstrap_decision_ms = self.start_ms - interval - 1
        else:
            bootstrap_decision_ms = self.start_ms - 1
        bootstrap_bars = [
            bar for bar in bars if bar["close_time_ms"] <= bootstrap_decision_ms
        ][-120:]
        return {
            "timeframe": timeframe,
            "interval_ms": interval,
            "expected": {
                "warmup_bars": warmup_bars,
                "evaluation_bars": (self.end_ms - self.start_ms) // interval,
                "total_bars": expected_count,
                "first_open_utc": _iso_ms(warmup_start_ms),
                "evaluation_start_utc": _iso_ms(self.start_ms),
                "evaluation_end_exclusive_utc": _iso_ms(self.end_ms),
            },
            "actual": {
                "warmup_bars": len(warmup),
                "evaluation_bars": len(evaluation),
                "total_bars": len(bars),
                "first_open_utc": _iso_ms(bars[0]["open_time_ms"]) if bars else None,
                "last_open_utc": _iso_ms(bars[-1]["open_time_ms"]) if bars else None,
                "first_close_utc": _iso_ms(bars[0]["close_time_ms"]) if bars else None,
                "last_close_utc": _iso_ms(bars[-1]["close_time_ms"]) if bars else None,
            },
            "first_decision_bootstrap": {
                "decision_time_utc": _iso_ms(bootstrap_decision_ms),
                "requested_closed_bars": 120,
                "returned_closed_bars": len(bootstrap_bars),
                "complete": len(bootstrap_bars) == 120,
                "oldest_returned_open_utc": (
                    _iso_ms(bootstrap_bars[0]["open_time_ms"])
                    if bootstrap_bars
                    else None
                ),
                "latest_returned_close_utc": (
                    _iso_ms(bootstrap_bars[-1]["close_time_ms"])
                    if bootstrap_bars
                    else None
                ),
            },
            "contiguous": contiguous,
            "missing_open_times_utc": [_iso_ms(stamp) for stamp in missing[:200]],
            "missing_count": len(missing),
            "unexpected_count": len(unexpected),
            "errors": errors,
            "pages": pages,
            "data_file": str(data_path),
            "data_sha256": _sha256(data_path),
        }

    async def run(self) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        results: dict[str, Any] = {}
        for timeframe in TIMEFRAMES:
            results[timeframe] = await self.fetch_timeframe(timeframe)

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at_utc": _iso_ms(time.time_ns() // 1_000_000),
            "source": {
                "service": "Hummingbot API",
                "client_method": "client.market_data.get_historical_candles",
                "endpoint": ENDPOINT,
                "connector": self.connector,
                "symbol": self.symbol,
                "read_only": True,
                "request_time_unit": "Unix seconds",
                "row_timestamp_interpreted_as": "candle open time",
            },
            "window": {
                "evaluation_start_utc": _iso_ms(self.start_ms),
                "evaluation_end_exclusive_utc": _iso_ms(self.end_ms),
                "evaluation_days": (self.end_ms - self.start_ms) // 86_400_000,
                "warmup_bars_per_timeframe": {
                    timeframe: (
                        self.one_minute_warmup_bars
                        if timeframe == "1m"
                        else self.warmup_bars
                    )
                    for timeframe in TIMEFRAMES
                },
                "pagination_max_bars": self.page_bars,
                "as_of_utc": _iso_ms(time.time_ns() // 1_000_000),
            },
            "timeframes": results,
        }
        manifest_path = self.output_dir / "manifest.json"
        _write_json(manifest_path, manifest)
        manifest["manifest_sha256"] = _sha256(manifest_path)
        # Write the manifest hash in a sidecar to avoid recursive self-hashing.
        (self.output_dir / "manifest.sha256").write_text(
            f"{manifest['manifest_sha256']}  manifest.json\n", encoding="ascii"
        )
        return manifest


def _load_credentials(config_path: Path, server_name: str) -> dict[str, Any]:
    """Read API credentials privately; this mapping is never logged or serialized."""
    import yaml

    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    entry = (parsed.get("servers") or {}).get(server_name)
    if not isinstance(entry, dict):
        raise ValueError(f"Hummingbot server {server_name!r} not found in config")
    return {
        "host": entry["host"],
        "port": entry["port"],
        "username": entry["username"],
        "password": entry["password"],
    }


async def _async_main(args: argparse.Namespace) -> int:
    # Keep these imports local to the CLI so importing FrozenHistoricalSource
    # does not need Hummingbot client setup or access to config secrets.
    from hummingbot_api_client import HummingbotAPIClient

    credentials = _load_credentials(args.config, args.server)
    client = HummingbotAPIClient(
        base_url=f"http://{credentials['host']}:{credentials['port']}",
        username=credentials["username"],
        password=credentials["password"],
    )
    await client.init()
    try:
        loader = HistoricalDataLoader(
            client,
            output_dir=args.output_dir,
            symbol=args.symbol,
            connector=args.connector,
            start_ms=_utc_ms(args.start),
            end_ms=_utc_ms(args.end),
            warmup_bars=args.warmup_bars,
            one_minute_warmup_bars=args.one_minute_warmup_bars,
            page_bars=args.page_bars,
        )
        manifest = await loader.run()
    finally:
        await client.close()

    complete = all(
        row["contiguous"] and row["first_decision_bootstrap"]["complete"]
        for row in manifest["timeframes"].values()
    )
    for timeframe, result in manifest["timeframes"].items():
        actual = result["actual"]
        print(
            f"{timeframe}: {actual['total_bars']}/{result['expected']['total_bars']} bars; "
            f"warmup={actual['warmup_bars']} eval={actual['evaluation_bars']} "
            f"contiguous={result['contiguous']} sha256={result['data_sha256']}"
        )
        if result["errors"]:
            for error in result["errors"]:
                print(
                    f"  source error {error['start_utc']}..{error['end_exclusive_utc']}: "
                    f"{error['error_type']}: {error['error']}"
                )
        if result["missing_count"]:
            print(f"  missing candles: {result['missing_count']}")
    print(f"manifest: {args.output_dir / 'manifest.json'}")
    print(f"source: {ENDPOINT} via {CONNECTOR if args.connector == CONNECTOR else args.connector}")
    return 0 if complete else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.yml")
    parser.add_argument("--server", default="local")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--connector", default=CONNECTOR)
    parser.add_argument("--start", default=EVALUATION_START)
    parser.add_argument("--end", default=EVALUATION_END, help="exclusive UTC boundary")
    parser.add_argument("--warmup-bars", type=int, default=DEFAULT_WARMUP_BARS)
    parser.add_argument(
        "--one-minute-warmup-bars",
        type=int,
        default=DEFAULT_ONE_MINUTE_WARMUP_BARS,
    )
    parser.add_argument("--page-bars", type=int, default=PAGE_BARS)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_async_main(args))
    except Exception as exc:
        print(f"walk-forward data fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
