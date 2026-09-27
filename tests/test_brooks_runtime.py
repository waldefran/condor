"""Brooks runtime lifecycle and durable event-state boundaries."""

from __future__ import annotations

import asyncio

from condor.agents.agent import Agent
from condor.agents.config import AgentConfig
from condor.agents.engine import TickEngine
from condor.agents.strategy import Strategy
from condor.brooks.config import BrooksConfig
from condor.brooks.supervisor import BrooksSupervisor


def test_brooks_config_keeps_loop_default_and_wake_defaults():
    assert AgentConfig().execution_mode == "loop"
    assert AgentConfig(execution_mode="brooks_agents").execution_mode == "brooks_agents"
    config = BrooksConfig.from_engine_config({})
    assert config.trader.timeframe == "1h"
    assert config.trader.wake_offset_sec == 2
    assert config.htf.timeframe == "1d"
    assert config.htf.wake_offset_sec == 3
    assert config.pm.frequency_sec == 60
    assert config.position_watcher.frequency_sec == 10


def test_brooks_engine_starts_without_tick_or_trade(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_AGENTS_ROOT", str(tmp_path / "agents"))
    monkeypatch.setenv("CONDOR_REPORTS_DIR", str(tmp_path / "reports"))
    strategy = Strategy(agent_slug="brooks_test", name="Runtime")
    strategy.home.mkdir(parents=True, exist_ok=True)
    engine = TickEngine(
        agent=Agent(slug="brooks_test", name="Brooks test", agent_key="test"),
        strategy=strategy,
        config={"execution_mode": "brooks_agents"},
        chat_id=1,
        user_id=1,
    )

    async def forbidden_tick():
        raise AssertionError("legacy tick was called")

    monkeypatch.setattr(engine, "_tick", forbidden_tick)

    async def exercise():
        await engine.start()
        assert engine.is_running
        assert isinstance(engine._brooks_supervisor, BrooksSupervisor)
        assert engine._brooks_supervisor.is_running
        engine.pause()
        assert engine.status == "paused"
        engine.resume()
        assert engine.status == "running"
        await engine.stop()
        assert not engine.is_running
        assert not engine._brooks_supervisor.is_running
        assert engine._task.done()

    asyncio.run(exercise())
