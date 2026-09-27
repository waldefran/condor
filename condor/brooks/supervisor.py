"""Isolated lifecycle owner for future independent Brooks workers.

FEAT-001 deliberately runs no trading, market-data or model tasks. Later
features register children here rather than routing Brooks through _tick().
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .config import BrooksConfig
from .events import EventBus
from .store import BrooksStore


class BrooksSupervisor:
    def __init__(self, strategy_home: Path, config: BrooksConfig):
        self.strategy_home = Path(strategy_home)
        self.config = config
        self._stop = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()
        self._children: set[asyncio.Task] = set()
        self._started = False
        self.store: BrooksStore | None = None
        self.events: EventBus | None = None

    @property
    def is_running(self) -> bool:
        return self._started and not self._stop.is_set()

    async def start(self) -> None:
        if self._started:
            return
        self.store = BrooksStore(self.strategy_home)
        self.events = EventBus(self.store)
        self._stop.clear()
        self._started = True

    async def run(self) -> None:
        await self._stop.wait()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    async def wait_until_resumed(self) -> None:
        await self._resume.wait()

    def add_child(self, task: asyncio.Task) -> None:
        """Give the supervisor ownership of a future worker task."""
        if not self.is_running:
            raise RuntimeError("Brooks supervisor is not running")
        self._children.add(task)
        task.add_done_callback(self._children.discard)

    async def stop(self) -> None:
        self._stop.set()
        children = tuple(self._children)
        for task in children:
            task.cancel()
        if children:
            await asyncio.gather(*children, return_exceptions=True)
        self._children.clear()
        if self.events is not None:
            self.events.close()
        if self.store is not None:
            self.store.flush()
        self._started = False
