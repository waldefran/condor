# Reproduções offline

Executadas contra HEAD 7760041a com readers/GM reais e venue/ExecutionPort falsos. Nenhuma chamada de rede ou write externo. Scripts registrados como evidência textual; resultados em lifecycle_probe.json e lifecycle_extended_probe.json.

## lifecycle_probe.py

```python
"""Read-only lifecycle probe. Uses production adapters with an in-memory fake venue."""
from __future__ import annotations
import asyncio, json, shutil, sys
sys.path.insert(0, '/home/valdemaster/brooks-condor/condor')
from pathlib import Path
from condor.brooks.adapters import HummingbotAccountReader, build_watcher_provider, build_pm_load_context, read_bindings
from condor.brooks.position_watcher import PositionWatcher
from condor.brooks.gm import GMPolicy, GMRejected, compile_main
from decimal import Decimal

BASE = Path('/tmp/brooks-investigation-20260930/baseline/docs/brooks_walkforward_2026-09-20_2026-09-29')
ROOT = Path('/tmp/brooks-investigation-20260930/lifecycle-probe-state')
CID = 'ETH-USDT-1h-1789876799999'
ACCOUNT, CONNECTOR, CONTROLLER, SYMBOL = 'walkforward', 'binance_perpetual_demo', 'brooks-walkforward-10d', 'ETH-USDT'
if ROOT.exists(): shutil.rmtree(ROOT)
trade_dir = ROOT / 'trades' / CID
trade_dir.mkdir(parents=True)
binding = json.loads((BASE/'trade_state'/CID/'binding.json').read_text())
trade = json.loads((BASE/'trade_state'/CID/'original_trade_intent.json').read_text())
(trade_dir/'binding.json').write_text(json.dumps(binding))
(trade_dir/'original_trade_intent.json').write_text(json.dumps(trade))

class Trading:
    def __init__(self, owner): self.o = owner
    async def get_positions(self, **kwargs): return {'data': [dict(r) for r in self.o.positions]}
    async def get_active_orders(self, **kwargs): return {'data': [dict(r) for r in self.o.orders]}
    async def get_position_mode(self, **kwargs): return {'position_mode':'HEDGE'}

class Executors:
    def __init__(self, owner): self.o = owner
    async def search_executors(self, **kwargs): return {'data':[dict(r) for r in self.o.executor_rows]}
    async def get_executor(self, executor_id=None):
        for r in self.o.executor_rows:
            if r.get('executor_id') == executor_id: return dict(r)
        raise RuntimeError(executor_id)

class MarketData:
    def __init__(self, owner): self.o=owner
    async def get_prices(self, **kwargs): return {'prices':{SYMBOL:'2595'}}

class Portfolio:
    async def get_state(self, **kwargs):
        return {ACCOUNT:{CONNECTOR:[{'token':'USDT','value':'10000','available':'9500'}]}}

class Connectors:
    async def get_trading_rules(self, connector_name, pairs):
        return {'trading_rules':{SYMBOL:{'min_base_amount_increment':'0.001','min_order_size':'0.001','min_notional_size':'5','max_leverage':5}}}

class Client:
    def __init__(self, positions, executors):
        self.positions, self.executors, self.orders = positions, executors, []
        self.trading, self.executors_router = Trading(self), Executors(self)
        self.market_data, self.portfolio, self.connectors = MarketData(self), Portfolio(), Connectors()
    @property
    def executors(self): return self.executors_router
    @executors.setter
    def executors(self, value): self._executor_rows = value
    @property
    def executor_rows(self): return self._executor_rows
    @executor_rows.setter
    def executor_rows(self, value): self._executor_rows=value

MAIN = {'position_id':'wf-main-1','trading_pair':SYMBOL,'position_side':'SHORT','net_amount_base':'0.478','current_price':'2584'}
HEDGE = {'position_id':'wf-hedge-1','trading_pair':SYMBOL,'position_side':'LONG','net_amount_base':'0.120','current_price':'2584'}
EXEC_MAIN = {'executor_id':'wf-exec-1','status':'RUNNING','account_name':ACCOUNT,'connector_name':CONNECTOR,'trading_pair':SYMBOL,'controller_id':CONTROLLER}
EXEC_HEDGE = {'executor_id':'wf-hedge-1','status':'RUNNING','account_name':ACCOUNT,'connector_name':CONNECTOR,'trading_pair':SYMBOL,'controller_id':CONTROLLER}

def build(client):
    reader = HummingbotAccountReader(client, ROOT, CONTROLLER, now_fn=lambda:1789916520000)
    provider = build_watcher_provider(client, account_name=ACCOUNT, connector_name=CONNECTOR,
        controller_id=CONTROLLER, symbols=[SYMBOL], state_root=ROOT)
    pm_load = build_pm_load_context(client, account_name=ACCOUNT, connector_name=CONNECTOR,
        controller_id=CONTROLLER, state_root=ROOT, now_fn=lambda:1789916520000)
    return reader, provider, pm_load

async def reader_summary(reader):
    s=await reader.read(account_name=ACCOUNT,connector_name=CONNECTOR,symbol=SYMBOL)
    return {'structure_status':s.structure_status,'open_positions':s.open_positions,
            'main_position_id':s.main_position_id,'main_quantity':str(s.main_quantity),
            'hedge_position_id':s.hedge_position_id,'hedge_quantity':str(s.hedge_quantity),
            'position_roles':[p.ownership_role for p in (s.positions or [])], 'gross_exposure':str(s.gross_exposure)}

async def main():
    out={}
    # Normal MAIN -> automatic close as observed by the read-only watcher.
    c=Client([MAIN],[EXEC_MAIN]); reader,provider,pm=build(c); seen=[]
    w=PositionWatcher(provider,seen.append)
    await w.poll(); out['open_main']={'reader':await reader_summary(reader),'watcher_events':[e['type'] for e in seen]}
    prior=await provider()
    c.positions=[]; c.executor_rows[0]['status']='CLOSED'; seen.clear()
    await w.poll(); out['closed_main_seeded']={'watcher_events':[e['type'] for e in seen],
        'persisted_binding':json.loads((trade_dir/'binding.json').read_text()),
        'read_bindings_count':len(read_bindings(ROOT,account_name=ACCOUNT,connector_name=CONNECTOR,controller_id=CONTROLLER)),
        'reader':await reader_summary(reader),'pm_context_is_none':(await pm(CID)) is None}
    policy=GMPolicy(risk_per_trade_pct=Decimal('.0005'),max_positions=1,max_gross_exposure_pct=Decimal('1'),
        leverage=5,take_profit_r=Decimal('2'),time_limit_sec=86400,max_snapshot_age_ms=60000,max_intent_age_ms=7200000)
    snap=await reader.read(account_name=ACCOUNT,connector_name=CONNECTOR,symbol=SYMBOL)
    try:
        compile_main(trade,snap,policy,now_ms=snap.as_of_ms)
        out['new_entry_after_close']='unexpectedly allowed'
    except GMRejected as e: out['new_entry_after_close']={'rejected':str(e)}
    # Production supervisor constructs a fresh watcher without initial snapshots.
    seen2=[]; fresh=PositionWatcher(provider,seen2.append); await fresh.poll()
    out['after_restart_unseeded']={'watcher_events':[e['type'] for e in seen2],
        'binding_still_readable':len(read_bindings(ROOT,account_name=ACCOUNT,connector_name=CONNECTOR,controller_id=CONTROLLER))==1,
        'pm_context_is_none':(await pm(CID)) is None}
    # MAIN closes while bound HEDGE remains: watcher sees MAIN closure but no hedge removal.
    hedge_binding=dict(binding,hedge_position_id='wf-hedge-1',hedge_executor_id='wf-hedge-1',hedge_size='0.120')
    (trade_dir/'binding.json').write_text(json.dumps(hedge_binding))
    c2=Client([MAIN,HEDGE],[EXEC_MAIN,EXEC_HEDGE]); r2,p2,pm2=build(c2); seen3=[]; w2=PositionWatcher(p2,seen3.append)
    await w2.poll(); c2.positions=[HEDGE]; c2.executor_rows[0]['status']='CLOSED'; seen3.clear(); await w2.poll()
    out['main_closed_hedge_open']={'watcher_events':[e['type'] for e in seen3],
        'reader':await reader_summary(r2),'pm_context_is_none':(await pm2(CID)) is None,
        'persisted_binding':json.loads((trade_dir/'binding.json').read_text())}
    print(json.dumps(out,indent=2,sort_keys=True))

asyncio.run(main())

```

## lifecycle_extended_probe.py

```python
from __future__ import annotations
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from decimal import Decimal

REPO = Path("/home/valdemaster/brooks-condor/condor")
sys.path.insert(0, str(REPO))

from condor.brooks.adapters import HummingbotAccountReader, read_bindings, _confirmed_executor
from condor.brooks.gm import BrooksGM, GMPolicy, GMRejected
import condor.brooks.gm as gm_module

BASE = Path("/tmp/brooks-investigation-20260930/baseline/docs/brooks_walkforward_2026-09-20_2026-09-29")
ROOT = Path("/tmp/brooks-investigation-20260930/lifecycle-extended-probe-state")
CID = "ETH-USDT-1h-1789876799999"
ACCOUNT = "walkforward"
CONNECTOR = "binance_perpetual_demo"
CONTROLLER = "brooks-walkforward-10d"
SYMBOL = "ETH-USDT"

if ROOT.exists():
    shutil.rmtree(ROOT)

class Trading:
    def __init__(self, owner):
        self.o = owner
    async def get_positions(self, **kwargs):
        return {"data": [dict(r) for r in self.o.positions]}
    async def get_active_orders(self, **kwargs):
        return {"data": [dict(r) for r in self.o.orders]}
    async def get_position_mode(self, **kwargs):
        return {"position_mode": "HEDGE"}

class Executors:
    def __init__(self, owner):
        self.o = owner
    async def search_executors(self, **kwargs):
        rows = self.o.executor_rows
        return {"data": [dict(r) for r in rows]}
    async def get_executor(self, executor_id=None):
        for r in self.o.executor_rows:
            if r.get("executor_id") == executor_id:
                return dict(r)
        raise RuntimeError(executor_id)

class MarketData:
    def __init__(self, owner):
        self.o = owner
    async def get_prices(self, **kwargs):
        return {"prices": {SYMBOL: "2584"}}

class Portfolio:
    async def get_state(self, **kwargs):
        return {ACCOUNT: {CONNECTOR: [{"token": "USDT", "value": "10000", "available": "9500"}]}}

class Connectors:
    async def get_trading_rules(self, connector_name, pairs):
        return {"trading_rules": {SYMBOL: {
            "min_base_amount_increment": "0.001",
            "min_order_size": "0.001",
            "min_notional_size": "5",
            "max_leverage": 5,
        }}}

class Client:
    def __init__(self, positions=None, executors=None):
        self.positions = list(positions or [])
        self.executor_rows = list(executors or [])
        self.orders = []
        self.trading = Trading(self)
        self.executors = Executors(self)
        self.market_data = MarketData(self)
        self.portfolio = Portfolio()
        self.connectors = Connectors()

class FakeExecution:
    controller_id = CONTROLLER
    def __init__(self, client=None):
        self.client = client
        self.open_calls = []
        self.close_calls = []
        self.hedge_calls = []
        self.reduce_calls = []
    async def open_main(self, **kwargs):
        self.open_calls.append(kwargs)
        return "unexpected-open"
    async def close_main(self, *, executor_id):
        self.close_calls.append(executor_id)
        # Successful fake ack followed by the venue becoming flat.
        if self.client is not None:
            self.client.positions = []
            for row in self.client.executor_rows:
                if row.get("executor_id") == executor_id:
                    row["status"] = "CLOSED"
        return executor_id
    async def execute_hedge(self, **kwargs):
        self.hedge_calls.append(kwargs)
        return "unexpected-hedge-write"
    async def reduce_main(self, **kwargs):
        self.reduce_calls.append(kwargs)
        return "unexpected-reduce"

def seed(root: Path, binding: dict, intent: dict | None = None):
    td = root / "trades" / str(binding["correlation_id"])
    td.mkdir(parents=True, exist_ok=True)
    (td / "binding.json").write_text(json.dumps(binding))
    if intent is not None:
        (td / "original_trade_intent.json").write_text(json.dumps(intent))

def base_binding(cid=CID):
    return {
        "schema": "condor.brooks.trade-binding.v1",
        "correlation_id": cid,
        "account_name": ACCOUNT,
        "connector_name": CONNECTOR,
        "controller_id": CONTROLLER,
        "symbol": SYMBOL,
        "main_side": "SHORT",
        "planned_quantity": "0.478",
        "status": "reconciled",
        "main_executor_id": "wf-exec-1",
        "executor_id": "wf-exec-1",
        "main_position_id": "wf-main-1",
    }

def exec_row(executor_id, status):
    return {
        "executor_id": executor_id,
        "status": status,
        "account_name": ACCOUNT,
        "connector_name": CONNECTOR,
        "trading_pair": SYMBOL,
        "controller_id": CONTROLLER,
    }

def main_position():
    return {
        "position_id": "wf-main-1",
        "trading_pair": SYMBOL,
        "position_side": "SHORT",
        "net_amount_base": "0.478",
        "current_price": "2584",
    }

def hedge_position():
    return {
        "position_id": "wf-hedge-1",
        "trading_pair": SYMBOL,
        "position_side": "LONG",
        "net_amount_base": "0.120",
        "current_price": "2584",
    }

def policy():
    return GMPolicy(
        risk_per_trade_pct=Decimal(".0005"),
        max_positions=1,
        max_gross_exposure_pct=Decimal("1"),
        leverage=5,
        take_profit_r=Decimal("2"),
        time_limit_sec=86400,
        max_snapshot_age_ms=60000,
        max_intent_age_ms=7200000,
    )

async def snapshot_dict(snapshot):
    return {
        "structure_status": snapshot.structure_status,
        "open_positions": snapshot.open_positions,
        "main_position_id": snapshot.main_position_id,
        "main_executor_id": snapshot.main_executor_id,
        "main_quantity": str(snapshot.main_quantity),
        "position_roles": [p.ownership_role for p in (snapshot.positions or [])],
        "gross_exposure": str(snapshot.gross_exposure),
    }

async def idless_case(name, executor_status):
    root = ROOT / name
    b = base_binding()
    b["main_position_id"] = "executor:wf-exec-1"
    seed(root, b)
    rows = [] if executor_status == "REMOVED" else [exec_row("wf-exec-1", executor_status)]
    client = Client([], rows)
    reader = HummingbotAccountReader(client, root, CONTROLLER, now_fn=lambda: int(time.time() * 1000))
    snap = await reader.read(account_name=ACCOUNT, connector_name=CONNECTOR, symbol=SYMBOL)
    confirmed = await _confirmed_executor(
        client, account_name=ACCOUNT, connector_name=CONNECTOR,
        controller_id=CONTROLLER, symbol=SYMBOL, executor_id="wf-exec-1",
    )
    intent = json.loads((BASE / "trade_state" / CID / "original_trade_intent.json").read_text())
    intent["decision_time_ms"] = int(time.time() * 1000)
    execution = FakeExecution()
    gm = BrooksGM(
        account_name=ACCOUNT, connector_name=CONNECTOR, state_root=root,
        policy=policy(), reader=reader, execution=execution,
    )
    try:
        await gm.execute_entry(intent, correlation_id=f"new-entry-{name}")
        entry = {"result": "unexpectedly_allowed"}
    except GMRejected as e:
        entry = {"rejected": str(e)}
    return {
        "executor_observation": executor_status,
        "confirmed_executor_identity": confirmed,
        "snapshot": await snapshot_dict(snap),
        "read_bindings_count": len(read_bindings(root, account_name=ACCOUNT, connector_name=CONNECTOR, controller_id=CONTROLLER)),
        "gm_entry": entry,
        "execution_open_calls": len(execution.open_calls),
    }

async def main():
    out = {}
    for case in ("CLOSED", "TERMINATED", "REMOVED"):
        out[f"idless_{case.lower()}"] = await idless_case(case.lower(), case)

    # Real BrooksGM CLOSE flow against a fake execution ack, then flat fake venue.
    close_root = ROOT / "pm-close"
    binding = base_binding()
    seed(close_root, binding)
    client = Client([main_position()], [exec_row("wf-exec-1", "RUNNING")])
    now = lambda: int(time.time() * 1000)
    reader = HummingbotAccountReader(client, close_root, CONTROLLER, now_fn=now)
    execution = FakeExecution(client)
    gm = BrooksGM(
        account_name=ACCOUNT, connector_name=CONNECTOR, state_root=close_root,
        policy=policy(), reader=reader, execution=execution,
    )
    before = await reader.read(account_name=ACCOUNT, connector_name=CONNECTOR, symbol=SYMBOL)
    binding_path = close_root / "trades" / CID / "binding.json"
    binding_before_bytes = binding_path.read_bytes()
    binding_before = json.loads(binding_before_bytes)
    close_result = await gm.execute_management(correlation_id=CID, decision_id="pm-close-ack", action="CLOSE")
    after = await reader.read(account_name=ACCOUNT, connector_name=CONNECTOR, symbol=SYMBOL)
    binding_after_bytes = binding_path.read_bytes()
    binding_after = json.loads(binding_after_bytes)
    out["pm_close_ack_then_flat"] = {
        "before": await snapshot_dict(before),
        "close_result": close_result,
        "fake_execution_close_calls": execution.close_calls,
        "fake_venue_positions_after_ack": len(client.positions),
        "after": await snapshot_dict(after),
        "binding_unchanged": binding_before == binding_after,
        "binding_file_bytes_unchanged": binding_before_bytes == binding_after_bytes,
        "persisted_binding_after": binding_after,
    }

    # MAIN absent, HEDGE still present: call the actual GM REMOVE_HEDGE route.
    # Limit retry delay/count in this isolated process; first fresh read proves orphan structure.
    orphan_root = ROOT / "orphan-remove"
    hedge_binding = base_binding()
    hedge_binding.update({
        "hedge_position_id": "wf-hedge-1",
        "hedge_executor_id": "wf-hedge-1",
        "hedge_size": "0.120",
    })
    seed(orphan_root, hedge_binding)
    client2 = Client([hedge_position()], [
        exec_row("wf-exec-1", "CLOSED"),
        exec_row("wf-hedge-1", "RUNNING"),
    ])
    reader2 = HummingbotAccountReader(client2, orphan_root, CONTROLLER, now_fn=now)
    execution2 = FakeExecution(client2)
    gm2 = BrooksGM(
        account_name=ACCOUNT, connector_name=CONNECTOR, state_root=orphan_root,
        policy=policy(), reader=reader2, execution=execution2,
    )
    pre = await reader2.read(account_name=ACCOUNT, connector_name=CONNECTOR, symbol=SYMBOL)
    gm_module._HEDGE_FRESH_READS = 1
    gm_module._HEDGE_FRESH_DELAY_SEC = 0
    try:
        await gm2.execute_management(
            correlation_id=CID,
            decision_id="orphan-remove-attempt",
            action="REMOVE_HEDGE",
            target_hedge_ratio="0",
            plan_main_position_id="wf-main-1",
            plan_hedge_position_id="wf-hedge-1",
        )
        remove = {"result": "unexpectedly_submitted"}
    except GMRejected as e:
        remove = {"rejected": str(e)}
    out["orphan_hedge_remove_attempt"] = {
        "pre_action_snapshot": await snapshot_dict(pre),
        "gm_remove_hedge": remove,
        "fake_execution_hedge_calls": len(execution2.hedge_calls),
        "binding_after_attempt": json.loads((orphan_root / "trades" / CID / "binding.json").read_text()),
    }

    path = Path("/tmp/brooks-investigation-20260930/lifecycle_extended_probe.json")
    path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(json.dumps(out, indent=2, sort_keys=True))

asyncio.run(main())


```

## provider_error_probe.py

Executado com `PYTHONPATH=. .venv/bin/python`, sem provider/source/venue.

```python
import asyncio,json
from condor.acp.pydantic_ai_client import PydanticAIClient
from condor.acp.client import TextChunk,PromptDone
from condor.brooks.agent_runner import _json_object
from condor.brooks.llm_coordination import is_transient_role_error
class Stub:
 def __init__(self,kind):self.kind=kind
 async def prompt_stream(self,text):
  if self.kind=='error':yield TextChunk('Provider transport error: connection reset')
  yield PromptDone(self.kind)
async def main():
 out={}
 for kind in ('timeout','error'):
  text=await PydanticAIClient.prompt(Stub(kind),'offline fixture')
  try:_json_object(text)
  except Exception as e:out[kind]={'aggregated_text':text,'host_exception':type(e).__name__,'host_message':str(e),'transient':is_transient_role_error(e)}
 print(json.dumps(out,indent=2))
asyncio.run(main())

```
