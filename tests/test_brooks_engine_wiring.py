"""Production wiring for execution_mode=brooks_agents (fakes, no live venue)."""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal

from condor.agents.agent import Agent
from condor.agents.engine import TickEngine
from condor.agents.strategy import Strategy
from condor.brooks.adapters import (
    HummingbotAccountReader,
    HummingbotCandleSource,
    build_execution_port,
    build_watcher_provider,
)
from condor.brooks.execution import HummingbotExecutionPort
from condor.brooks.gm import AccountSnapshot, BrooksGM, GMRejected
from condor.brooks.market_tools import ClosedBarGate

HOUR_MS = 3_600_000


class FakeMarketData:
    def __init__(self, outer):
        self._outer = outer

    async def get_candles(self, connector_name, trading_pair, interval, max_records=None):
        self._outer.calls.append(("get_candles", trading_pair, interval))
        return {"data": [dict(row) for row in self._outer.candle_rows]}

    async def get_historical_candles(
        self, connector_name, trading_pair, interval, start_time=None, end_time=None
    ):
        self._outer.calls.append(("get_historical_candles", trading_pair, interval))
        return {"data": [dict(row) for row in self._outer.candle_rows]}

    async def get_prices(self, connector_name, trading_pairs):
        self._outer.calls.append(("get_prices", tuple(trading_pairs)))
        return {"prices": dict(self._outer.prices)}


class FakeTrading:
    def __init__(self, outer):
        self._outer = outer

    async def get_positions(self, account_names=None, connector_names=None, limit=50):
        self._outer.calls.append(("get_positions",))
        if self._outer.fail_venue:
            raise ConnectionError("venue down")
        return {"data": [dict(row) for row in self._outer.positions]}

    async def get_active_orders(
        self, account_names=None, connector_names=None, trading_pairs=None, limit=50
    ):
        self._outer.calls.append(("get_active_orders",))
        if self._outer.fail_venue:
            raise ConnectionError("venue down")
        return {"data": [dict(row) for row in self._outer.orders]}

    async def get_position_mode(self, account_name, connector_name):
        self._outer.calls.append(("get_position_mode",))
        return {"position_mode": "HEDGE"}


class FakeExecutors:
    def __init__(self, outer):
        self._outer = outer

    async def search_executors(
        self,
        account_names=None,
        connector_names=None,
        trading_pairs=None,
        controller_ids=None,
        limit=50,
    ):
        self._outer.calls.append(("search_executors",))
        if self._outer.fail_venue:
            raise ConnectionError("venue down")
        return {"data": [dict(row) for row in self._outer.executor_rows]}

    async def get_executor(self, executor_id=None):
        self._outer.calls.append(("get_executor", executor_id))
        for row in self._outer.executor_rows:
            if row.get("executor_id") == executor_id or row.get("id") == executor_id:
                return dict(row)
        raise RuntimeError(f"executor {executor_id} not found")


class FakePortfolio:
    def __init__(self, outer):
        self._outer = outer

    async def get_state(
        self, account_names=None, connector_names=None, skip_gateway=False
    ):
        self._outer.calls.append(("get_state",))
        return json.loads(json.dumps(self._outer.portfolio_state))


class FakeConnectors:
    def __init__(self, outer):
        self._outer = outer

    async def get_trading_rules(self, connector_name, trading_pairs=None):
        self._outer.calls.append(("get_trading_rules",))
        return json.loads(json.dumps(self._outer.rules))


class FakeClient:
    """Duck-typed Hummingbot client; records every venue touch."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.candle_rows: list[dict] = []
        self.prices: dict = {}
        self.positions: list[dict] = []
        self.executor_rows: list[dict] = []
        self.orders: list[dict] = []
        self.portfolio_state: dict = {}
        self.rules: dict = {}
        self.fail_venue = False
        self.market_data = FakeMarketData(self)
        self.trading = FakeTrading(self)
        self.executors_router = FakeExecutors(self)
        self.portfolio_router = FakePortfolio(self)
        self.connectors = FakeConnectors(self)

    @property
    def executors(self):
        return self.executors_router

    @property
    def portfolio(self):
        return self.portfolio_router


def closed_row(open_ms, price):
    return {
        "timestamp": open_ms / 1000,
        "open": float(price),
        "high": float(price + 2),
        "low": float(price - 1),
        "close": float(price + 1),
        "volume": 10.0,
    }


def make_strategy(tmp_path, monkeypatch, name="Wiring"):
    monkeypatch.setenv("CONDOR_AGENTS_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("CONDOR_REPORTS_DIR", str(tmp_path / "reports"))
    strategy = Strategy(agent_slug="brooks_wiring", name=name)
    strategy.home.mkdir(parents=True, exist_ok=True)
    agent = Agent(slug="brooks_wiring", name="Brooks wiring", agent_key="fallback-key")
    return agent, strategy


def wiring_config():
    return {
        "execution_mode": "brooks_agents",
        "agent_key": "test-key",
        "brooks": {
            "symbols": ["BTC-USDT"],
            "account_name": "acct",
            "connector_name": "binance_perpetual",
        },
    }


def test_wiring_attaches_production_collaborators(tmp_path, monkeypatch):
    agent, strategy = make_strategy(tmp_path, monkeypatch)
    engine = TickEngine(
        agent=agent, strategy=strategy, config=wiring_config(), chat_id=1, user_id=1
    )
    fake = FakeClient()

    async def fake_client():
        return fake

    monkeypatch.setattr(engine, "_get_client", fake_client)

    async def exercise():
        await engine.start()
        sup = engine._brooks_supervisor
        assert sup is not None and sup.is_running
        assert sup._symbols == ["BTC-USDT"]
        assert isinstance(sup._candle_source, HummingbotCandleSource)
        assert not hasattr(sup._candle_source, "client")
        assert sup._agent_key == "test-key"
        assert sup._user_id == 1
        assert callable(sup._gm_factory)
        assert callable(sup._watcher_snapshots)
        gm = sup._gm_factory("BTC-USDT")
        assert isinstance(gm, BrooksGM)
        assert isinstance(gm.reader, HummingbotAccountReader)
        assert isinstance(gm.execution, HummingbotExecutionPort)
        await engine.stop()

    asyncio.run(exercise())


def test_missing_config_starts_inert_without_writes(tmp_path, monkeypatch, caplog):
    agent, strategy = make_strategy(tmp_path, monkeypatch)
    engine = TickEngine(
        agent=agent,
        strategy=strategy,
        config={"execution_mode": "brooks_agents"},
        chat_id=1,
        user_id=1,
    )
    fake = FakeClient()

    async def fake_client():
        return fake

    monkeypatch.setattr(engine, "_get_client", fake_client)

    async def exercise():
        with caplog.at_level(logging.ERROR, logger="condor.agents.engine"):
            await engine.start()
        sup = engine._brooks_supervisor
        assert sup is not None and sup.is_running
        assert sup._symbols == []
        assert sup._candle_source is None
        assert sup._gm_factory is None
        assert sup._watcher_snapshots is None
        await engine.stop()

    asyncio.run(exercise())
    assert fake.calls == []
    assert any(
        "brooks_agents is not configured" in record.message
        for record in caplog.records
    )


def test_unreachable_server_starts_inert_without_exception(
    tmp_path, monkeypatch, caplog
):
    agent, strategy = make_strategy(tmp_path, monkeypatch)
    engine = TickEngine(
        agent=agent, strategy=strategy, config=wiring_config(), chat_id=1, user_id=1
    )

    async def no_client():
        return None

    monkeypatch.setattr(engine, "_get_client", no_client)

    async def exercise():
        with caplog.at_level(logging.ERROR, logger="condor.agents.engine"):
            await engine.start()
        sup = engine._brooks_supervisor
        assert sup is not None and sup.is_running
        assert sup._candle_source is None
        await engine.stop()

    asyncio.run(exercise())
    assert any(
        "no accessible Hummingbot server" in record.message
        for record in caplog.records
    )


def test_candle_adapter_obeys_closed_bar_gate():
    now_ms = 172_800_000 * 200
    last_close = now_ms - (now_ms % HOUR_MS) - 1
    rows = [
        closed_row(last_close - 2 * HOUR_MS + 1, 100),
        closed_row(last_close - HOUR_MS + 1, 101),
        closed_row(last_close + 1, 102),  # forming bar: close in the future
    ]
    fake = FakeClient()
    fake.candle_rows = rows
    source = HummingbotCandleSource(
        fake, "binance_perpetual", now_fn=lambda: now_ms
    )

    async def exercise():
        return await source.fetch_candles("BTC-USDT", "1h", 2)

    bars = asyncio.run(exercise())
    assert len(bars) == 2
    assert all(bar["closed"] is True for bar in bars)
    assert bars[-1]["close_time_ms"] == last_close
    gate = ClosedBarGate(timeframe="1h", decision_time_ms=last_close)
    validated = gate.validate(bars, required_count=2, trigger_timeframe=True)
    assert [bar.close_time_ms for bar in validated] == [
        bar["close_time_ms"] for bar in bars
    ]


def test_delayed_restart_reads_exact_h1_and_m15_history_at_decision_time(monkeypatch):
    from condor.brooks.market_tools import TraderMarketTools

    decision_boundary_ms = 200 * HOUR_MS
    decision_time_ms = decision_boundary_ms - 1
    restart_time_ms = decision_time_ms + 43 * 60_000
    intervals_ms = {"1h": HOUR_MS, "15m": 900_000}
    available_rows = {}
    for timeframe, interval_ms in intervals_ms.items():
        rows = [
            closed_row(decision_boundary_ms - (120 - index) * interval_ms, 100 + index)
            for index in range(120)
        ]
        # Later bars are available to a delayed live read, but must not enter
        # the decision-time snapshot.
        rows.extend(
            closed_row(decision_boundary_ms + index * interval_ms, 300 + index)
            for index in range(4)
        )
        available_rows[timeframe] = rows

    queries = []

    async def fake_fetch_historical_candles(
        _client,
        _connector,
        _symbol,
        timeframe,
        *,
        start_time,
        end_time,
        limit,
    ):
        queries.append((timeframe, start_time, end_time, limit))
        return [
            row
            for row in available_rows[timeframe]
            if start_time <= row["timestamp"] <= end_time
        ]

    monkeypatch.setattr(
        "condor.fetchers.market_data.fetch_historical_candles",
        fake_fetch_historical_candles,
    )
    source = HummingbotCandleSource(
        FakeClient(), "binance_perpetual", now_fn=lambda: restart_time_ms
    )
    tools = TraderMarketTools(source=source, decision_time_ms=decision_time_ms)

    async def exercise():
        return {
            timeframe: await tools.get_closed_candles("BTC-USDT", timeframe, 120)
            for timeframe in ("1h", "15m")
        }

    bars_by_timeframe = asyncio.run(exercise())
    assert [query[0] for query in queries] == ["1h", "15m"]
    assert all(query[2] == decision_time_ms // 1000 for query in queries)
    for timeframe, bars in bars_by_timeframe.items():
        assert len(bars) == 120
        assert bars[-1]["close_time_ms"] == decision_time_ms
        assert all(bar["close_time_ms"] <= decision_time_ms for bar in bars)


def test_account_reader_reports_flat_book_without_bindings(tmp_path):
    fake = FakeClient()
    fake.prices = {"BTC-USDT": 50000.0}
    fake.portfolio_state = {
        "acct": {
            "binance_perpetual": [
                {"token": "USDC", "value": 1000.0},
                {"token": "BTC", "value": 500.0},
            ]
        }
    }
    fake.rules = {
        "trading_rules": {
            "BTC-USDT": {
                "min_base_amount_increment": "0.001",
                "min_order_size": "0.001",
                "min_notional": "10",
                "max_leverage": 10,
            }
        }
    }
    reader = HummingbotAccountReader(fake, tmp_path, "ctrl")

    async def exercise():
        return await reader.read(
            account_name="acct", connector_name="binance_perpetual", symbol="BTC-USDT"
        )

    snapshot = asyncio.run(exercise())
    assert isinstance(snapshot, AccountSnapshot)
    assert snapshot.structure_status == "no_positions"
    assert snapshot.equity == 1500
    assert snapshot.available_margin == 1000
    assert snapshot.mark_price == 50000
    assert snapshot.main_position_id is None


def test_executor_confirmation_prefers_direct_read_and_checks_scope(tmp_path):
    from condor.brooks.adapters import _confirmed_executor

    fake = FakeClient()
    fake.executor_rows = [
        {
            "executor_id": "e1",
            "status": "RUNNING",
            "account_name": "acct",
            "connector_name": "binance_perpetual",
            "trading_pair": "BTC-USDT",
            "controller_id": "ctrl",
        }
    ]

    async def exercise(**kwargs):
        return await _confirmed_executor(fake, **kwargs)

    base = dict(
        account_name="acct",
        connector_name="binance_perpetual",
        controller_id="ctrl",
        symbol="BTC-USDT",
        executor_id="e1",
    )
    assert asyncio.run(exercise(**base)) is True
    assert ("get_executor", "e1") in fake.calls
    assert not any(call[0] == "search_executors" for call in fake.calls)
    wrong = dict(base, controller_id="someone-else")
    assert asyncio.run(exercise(**wrong)) is False
    missing = dict(base, executor_id="nope")
    assert asyncio.run(exercise(**missing)) is False


def test_reader_resolves_creator_paired_hedge_on_id_less_venue(tmp_path):
    trades = tmp_path / "trades" / "trade-1"
    trades.mkdir(parents=True)
    (trades / "binding.json").write_text(
        json.dumps(
            {
                "schema": "condor.brooks.trade-binding.v1",
                "correlation_id": "trade-1",
                "account_name": "acct",
                "connector_name": "binance_perpetual",
                "controller_id": "ctrl",
                "symbol": "BTC-USDT",
                "main_side": "LONG",
                "main_position_id": "executor:exec-main-1",
                "main_executor_id": "exec-main-1",
                "hedge_position_id": "executor:exec-h1",
                "hedge_executor_id": "exec-h1",
                "hedge_size": "0.3",
                "status": "reconciled",
            }
        )
    )
    fake = FakeClient()
    fake.prices = {"BTC-USDT": 50000.0}
    fake.positions = [
        {
            "trading_pair": "BTC-USDT",
            "side": "LONG",
            "amount": 1.0,
            "entry_price": 49000.0,
        },
        {
            "trading_pair": "BTC-USDT",
            "side": "SHORT",
            "amount": -0.3,
            "entry_price": 49500.0,
        },
    ]
    fake.executor_rows = [
        {
            "executor_id": "exec-main-1",
            "status": "RUNNING",
            "account_name": "acct",
            "connector_name": "binance_perpetual",
            "trading_pair": "BTC-USDT",
            "controller_id": "ctrl",
        },
        {
            "executor_id": "exec-h1",
            "status": "TERMINATED",
            "account_name": "acct",
            "connector_name": "binance_perpetual",
            "trading_pair": "BTC-USDT",
            "controller_id": "ctrl",
        },
    ]
    fake.portfolio_state = {
        "acct": {
            "binance_perpetual": [
                {"token": "USDC", "value": 10000.0},
            ]
        }
    }
    fake.rules = {
        "BTC-USDT": {
            "min_base_amount_increment": "0.001",
            "min_order_size": "0.001",
            "min_notional_size": "10",
        }
    }
    reader = HummingbotAccountReader(fake, tmp_path, "ctrl")

    async def exercise():
        return await reader.read(
            account_name="acct", connector_name="binance_perpetual", symbol="BTC-USDT"
        )

    snapshot = asyncio.run(exercise())
    assert snapshot.structure_status == "single_main"
    assert snapshot.main_position_id == "executor:exec-main-1"
    assert snapshot.main_quantity == Decimal("1.0")
    assert snapshot.hedge_position_id == "executor:exec-h1"
    assert snapshot.hedge_quantity == Decimal("0.3")
    assert snapshot.rules.max_leverage is None


def test_account_reader_resolves_main_from_binding_only(tmp_path):
    trades = tmp_path / "trades" / "trade-1"
    trades.mkdir(parents=True)
    (trades / "binding.json").write_text(
        json.dumps(
            {
                "schema": "condor.brooks.trade-binding.v1",
                "correlation_id": "trade-1",
                "account_name": "acct",
                "connector_name": "binance_perpetual",
                "controller_id": "ctrl",
                "symbol": "BTC-USDT",
                "main_position_id": "p1",
                "main_executor_id": "e1",
                "main_side": "LONG",
                "status": "submitted",
            }
        )
    )
    fake = FakeClient()
    fake.prices = {"BTC-USDT": 50000.0}
    fake.positions = [
        {
            "position_id": "p1",
            "trading_pair": "BTC-USDT",
            "position_side": "LONG",
            "net_amount_base": "0.1",
            "current_price": "50000",
        }
    ]
    fake.portfolio_state = {
        "acct": {"binance_perpetual": [{"token": "USDC", "value": 1000.0}]}
    }
    fake.rules = {
        "trading_rules": {
            "BTC-USDT": {
                "min_base_amount_increment": "0.001",
                "min_order_size": "0.001",
                "min_notional": "10",
                "max_leverage": 10,
            }
        }
    }
    reader = HummingbotAccountReader(fake, tmp_path, "ctrl")

    async def exercise():
        return await reader.read(
            account_name="acct", connector_name="binance_perpetual", symbol="BTC-USDT"
        )

    snapshot = asyncio.run(exercise())
    assert snapshot.structure_status == "single_main"
    assert snapshot.main_position_id == "p1"
    assert snapshot.main_side == "LONG"


def test_account_reader_rejects_incomplete_venue_state(tmp_path):
    fake = FakeClient()
    fake.prices = {"BTC-USDT": 50000.0}
    fake.portfolio_state = {}
    fake.rules = {
        "trading_rules": {
            "BTC-USDT": {
                "min_base_amount_increment": "0.001",
                "min_order_size": "0.001",
                "min_notional": "10",
                "max_leverage": 10,
            }
        }
    }
    reader = HummingbotAccountReader(fake, tmp_path, "ctrl")

    async def exercise():
        return await reader.read(
            account_name="acct", connector_name="binance_perpetual", symbol="BTC-USDT"
        )

    try:
        asyncio.run(exercise())
    except GMRejected:
        return
    raise AssertionError("expected GMRejected for missing balances")


def test_watcher_provider_emits_bound_snapshot_and_idles_on_failure(tmp_path):
    trades = tmp_path / "trades" / "trade-1"
    trades.mkdir(parents=True)
    (trades / "binding.json").write_text(
        json.dumps(
            {
                "schema": "condor.brooks.trade-binding.v1",
                "correlation_id": "trade-1",
                "account_name": "acct",
                "connector_name": "binance_perpetual",
                "controller_id": "ctrl",
                "symbol": "BTC-USDT",
                "main_position_id": "p1",
                "main_side": "LONG",
                "status": "submitted",
            }
        )
    )
    fake = FakeClient()
    fake.positions = [
        {
            "position_id": "p1",
            "trading_pair": "BTC-USDT",
            "position_side": "LONG",
            "net_amount_base": "0.1",
            "current_price": "50000",
        }
    ]
    provider = build_watcher_provider(
        fake,
        account_name="acct",
        connector_name="binance_perpetual",
        controller_id="ctrl",
        symbols=["BTC-USDT"],
        state_root=tmp_path,
    )
    snapshots = asyncio.run(provider())
    assert len(snapshots) == 1
    assert snapshots[0]["correlation_id"] == "trade-1"
    assert snapshots[0]["main"]["id"] == "p1"
    assert snapshots[0]["main"]["qty"] == "0.1"
    fake.fail_venue = True
    assert asyncio.run(provider()) == []


def test_execution_port_reuses_existing_write_path():
    fake = FakeClient()
    port = build_execution_port(
        fake,
        account_name="acct",
        connector_name="binance_perpetual",
        controller_id="ctrl",
    )
    assert isinstance(port, HummingbotExecutionPort)
    assert port.controller_id == "ctrl"


def test_loop_mode_never_touches_brooks_wiring(tmp_path, monkeypatch):
    agent, strategy = make_strategy(tmp_path, monkeypatch)
    engine = TickEngine(
        agent=agent,
        strategy=strategy,
        config={"execution_mode": "loop", "frequency_sec": 3600},
        chat_id=1,
        user_id=1,
    )

    async def forbidden_wire():
        raise AssertionError("brooks wiring ran in loop mode")

    async def idle_tick():
        return None

    monkeypatch.setattr(engine, "_wire_brooks_production", forbidden_wire)
    monkeypatch.setattr(engine, "_tick", idle_tick)

    async def exercise():
        await engine.start()
        assert engine._brooks_supervisor is None
        assert engine.is_running
        await engine.stop()

    asyncio.run(exercise())
