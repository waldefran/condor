# CONDOR + BROOKS AGENTS V1
## Plano de Implementação Rápida — Trader + HTF Analyst + GM + Position PM + Hedge

**Status:** implementation-ready
**Base principal:** Condor
**Execução inicial:** Hummingbot API / Hummingbot MCP
**Conhecimento:** Skill-Brooks
**Referência de contratos/gates:** brooks-harness

---

# 0. Objetivo

Implementar **dentro do Condor**, sem substituir o Condor e sem criar outro harness, um modo novo de operação com agentes independentes:

```text
                         EVENT BUS
                             │
       ┌─────────────────────┼──────────────────────┐
       │                     │                      │
 H1_BAR_CLOSED         D1_BAR_CLOSED         TIMER / POSITION
       │                     │                      │
       ▼                     ▼                      ▼
 ┌───────────┐        ┌─────────────┐        ┌─────────────┐
 │  TRADER   │        │ HTF ANALYST │        │     PM      │
 │   AGENT   │        │    AGENT    │        │    AGENT    │
 └─────┬─────┘        └──────┬──────┘        └──────┬──────┘
       │                     │                      │
       │ tools               │ tools                │ tools
       │ skills              │ skills               │ skills
       ▼                     ▼                      ▼
 TraderIntent          MarketContext        ManagementIntent
       │                     │                      │
       └─────────────┬───────┴───────────┬──────────┘
                     ▼                   ▼
                 STATE STORE        EVENT STORE
                     │
                     ▼
             deterministic GM
                     │
                     ▼
               EXECUTION PORT
                     │
                     ▼
                Hummingbot
                     │
                     ▼
              Exchange venue
```

No V1:

- Condor continua sendo o runtime/harness;
- Hummingbot API continua sendo o braço mecânico;
- Trader apenas analisa mercado e emite intenção;
- HTF Analyst apenas produz contexto de prazo maior;
- GM é determinístico e controla conta, sizing, risco e mutações;
- Position PM é LLM agentic, lê skills, chama ferramentas e gerencia posições;
- PM pode criar, aumentar, reduzir e remover **hedge**, mas nunca escreve direto na exchange;
- todas as mutações passam pelo GM;
- o modo existente do Condor continua intacto.

---

# 1. Não negociar a arquitetura

## 1.1 Condor continua sendo o sistema

Não criar:

- novo harness;
- novo daemon separado;
- novo framework de agentes;
- Hermes/Pi como dependência obrigatória;
- fork da Hummingbot API só para suportar o V1.

Usar o que já existe no Condor:

- `TickEngine`;
- lifecycle start/stop/pause;
- `LoopSupervisor`;
- ACP/Pydantic-AI;
- model selection via `agent_key`;
- skills;
- MCP;
- tool allowlist/mutes;
- Hummingbot server resolution;
- journal;
- session directories;
- risk infrastructure;
- Hummingbot executor tools.

Criar apenas um modo novo:

```yaml
execution_mode: brooks_agents
```

O modo atual:

```yaml
execution_mode: loop
```

não pode mudar de semântica.

---

## 1.2 Separação estrita dos papéis

### Trader

Pergunta:

> O mercado justifica LONG, SHORT ou nenhuma entrada agora?

Trader não sabe:

- saldo;
- equity;
- margem;
- posições existentes;
- PnL;
- ordens;
- executores;
- drawdown;
- tamanho disponível.

Trader não executa.

Output:

```text
brooks.trade-intent.v2
```

com:

```text
ENTER_LONG
ENTER_SHORT
NO_TRADE
```

O Trader continua sendo executado mesmo quando já existe posição aberta.

A opinião atual do Trader é informação para o PM.

---

### HTF Analyst

Pergunta:

> Qual é o contexto atual de prazo maior?

Não abre posição.

Não conhece saldo ou posição.

Output:

```text
brooks.market-context.v1
```

Acorda inicialmente em:

```text
D1_BAR_CLOSED
```

Depois podemos adicionar H4 sem alterar os outros papéis.

---

### GM

Pergunta:

> Esta intenção pode virar ação real? Qual quantidade é permitida?

GM **não é LLM no V1**.

Responsável por:

- saldo;
- equity;
- margem;
- max positions;
- sizing;
- leverage policy;
- risk per trade;
- exposure;
- posição MAIN/HEDGE;
- hedge ratio;
- quantização;
- validação de estado;
- serialização de writes;
- tradução para Hummingbot.

Nenhum LLM escreve na exchange.

---

### Position PM

Pergunta:

> Dado o estado atual da operação e o que os analistas estão dizendo, qual é a melhor ação de gestão agora?

É um agente LLM de verdade:

```text
wake
→ lê estado
→ lê skill
→ chama tools
→ raciocina
→ chama tools adicionais se necessário
→ retorna ManagementDecision
```

Não é um `if pnl < X`.

Output:

```text
brooks.management-decision.v2
```

---

# 2. Ferramentas do PM: contexto pequeno, investigação sob demanda

O PM **não recebe centenas de candles no prompt inicial**.

Input inicial:

```text
position snapshot
executor snapshot
open orders
recent fills
PnL / costs
original TradeIntent
latest TradeIntent
latest HTF MarketContext
management history
management policy
hedge state
margin health
```

O PM pode chamar:

```text
get_market_context()
get_recent_structure()
get_volatility()
get_latest_trader_intent()
get_original_trade_intent()

get_position_state()
get_executor_state()
get_open_orders()
get_recent_fills()

get_candles(symbol, timeframe, limit <= 30)
```

`get_candles()` para PM:

- somente closed bars;
- passa pelo ClosedBarGate;
- `limit <= 30`;
- apenas timeframes permitidos;
- decisão registrada no Event Store;
- sem acesso a dados futuros;
- sem cliente Hummingbot bruto.

Isso permite ao PM raciocinar como um coding agent sem duplicar o Trader.

---

# 3. Hedge no V1 — sem alterar Hummingbot API

O V1 deve suportar:

```text
HEDGE
INCREASE_HEDGE
REDUCE_HEDGE
REMOVE_HEDGE
```

Além de:

```text
HOLD
REDUCE
CLOSE
REQUEST_MARKET_ANALYSIS
RECONCILE_STATE
MANAGEMENT_BLOCKED
```

## 3.1 Infraestrutura que já existe

No Condor/Hummingbot atual:

- existe configuração `PositionMode.HEDGE`;
- existe operação para `HEDGE` / `ONEWAY`;
- `create_order_executor` aceita `position_action`;
- `position_action` suporta `OPEN` e `CLOSE`;
- Hummingbot já possui controller oficial que usa `PositionMode.HEDGE` e abre/reduz hedge com `PositionAction.OPEN/CLOSE`.

Portanto:

**não alterar a Hummingbot API para o hedge V1.**

---

## 3.2 MAIN

A entrada principal continua preferencialmente via:

```text
create_position_executor
```

porque já entrega:

```text
entry
stop loss
take profit
time limit
controller_id
```

Exemplo:

```text
MAIN LONG:
BUY
create_position_executor
```

---

## 3.3 Abrir hedge

Exemplo:

```text
MAIN:
BTC LONG 1.0

PM:
HEDGE target_hedge_ratio = 0.30
```

GM calcula:

```text
target hedge notional
current hedge notional
delta hedge notional
quantized quantity
margin impact
```

Execution:

```text
create_order_executor
side = SELL
position_action = OPEN
```

Resultado:

```text
MAIN  LONG
HEDGE SHORT
```

---

## 3.4 Aumentar hedge

```text
current hedge ratio = 0.30
target hedge ratio  = 0.50
```

GM calcula somente o delta:

```text
delta = target hedge - current hedge
```

Execution:

```text
SELL
position_action = OPEN
```

---

## 3.5 Reduzir hedge

```text
current hedge ratio = 0.50
target hedge ratio  = 0.20
```

Execution:

```text
BUY
position_action = CLOSE
```

somente na quantidade correspondente ao delta.

---

## 3.6 Remover hedge

```text
target_hedge_ratio = 0
```

Execution:

```text
BUY full hedge quantity
position_action = CLOSE
```

MAIN permanece aberto.

---

# 4. MAIN e HEDGE nunca são inferidos pelo LLM

Persistir ownership explícito.

Estado canônico:

```json
{
  "schema": "condor.brooks.hedge-state.v1",
  "structure_status": "ok",
  "unresolved": false,

  "main_position_id": "main-123",
  "hedge_position_id": "hedge-456",

  "main_side": "LONG",
  "hedge_side": "SHORT",

  "main_size": "1.0",
  "hedge_size": "0.3",

  "hedge_ratio": "0.30",
  "ratio_basis": "absolute_mark_notional",

  "net_exposure_usd": "70000",
  "gross_exposure_usd": "130000"
}
```

Não inferir MAIN/HEDGE por:

- side;
- array order;
- tamanho;
- PnL;
- qual abriu primeiro;
- opinião do modelo.

Se ownership não puder ser resolvido:

```text
structure_status != ok/single_main
```

ações de hedge são bloqueadas.

PM deve retornar:

```text
RECONCILE_STATE
```

ou:

```text
MANAGEMENT_BLOCKED
```

---

# 5. Configurar Hedge Mode

Antes da primeira mutação Brooks em perpetual:

```text
ensure position mode == HEDGE
ensure leverage policy
```

Usar a primitive já existente no Hummingbot/Condor para:

```text
set_position_mode_and_leverage
```

Não ficar alternando HEDGE ↔ ONEWAY durante o ciclo.

O modo da conta/connector é requisito operacional do run.

Se não puder confirmar Hedge Mode:

```text
HEDGE actions = blocked
```

A entrada MAIN pode seguir somente se a policy permitir e estiver consistente com o modo atual.

---

# 6. Estrutura nova no Condor

Criar:

```text
condor/
└── brooks/
    ├── __init__.py
    ├── config.py
    ├── contracts.py
    ├── events.py
    ├── store.py
    ├── market_tools.py
    ├── agent_runner.py
    ├── trader.py
    ├── htf_analyst.py
    ├── pm.py
    ├── gm.py
    ├── hedge.py
    ├── execution.py
    ├── position_watcher.py
    └── supervisor.py
```

Novo agente:

```text
agents/
└── brooks_price_action/
    ├── AGENT.md
    ├── skills/
    │   ├── brooks-market-context/
    │   ├── brooks-trade-entry/
    │   └── brooks-position-management/
    └── strategies/
        └── btc_usdt_brooks/
            └── strategy.md
```

Reutilizar/copy-in das skills de `Skill-Brooks` conforme o padrão atual do Condor.

Não editar os agentes existentes.

---

# 7. Novo lifecycle

O `TickEngine` continua sendo a entrada do lifecycle.

Não transformar o `_tick()` normal em:

```text
PM → Trader → GM
```

Isso seria um pipeline sequencial e não é o desejado.

Para:

```text
execution_mode == brooks_agents
```

iniciar um:

```text
BrooksSupervisor
```

com tasks independentes:

```text
BrooksSupervisor
│
├── MarketClock
│   ├── H1 publisher
│   └── D1 publisher
│
├── TraderConsumer
├── HTFConsumer
│
├── PMTimer
├── PositionWatcher
├── PMConsumer
│
└── GMConsumer
```

No stop:

```text
cancel tasks
wait children
flush state
flush event store
final journal
```

---

# 8. Event Bus

Arquivo:

```text
condor/brooks/events.py
```

MVP:

```python
asyncio.Queue
```

Não adicionar Redis/Kafka/NATS.

Eventos:

```text
H1_BAR_CLOSED
D1_BAR_CLOSED

TRADER_INTENT_CREATED
MARKET_CONTEXT_UPDATED

PM_TIMER

POSITION_OPENED
POSITION_CHANGED
POSITION_CLOSED
ORDER_CHANGED
FILL

MANAGEMENT_INTENT_CREATED

GM_ENTRY_APPROVED
GM_ENTRY_REJECTED

GM_MANAGEMENT_APPROVED
GM_MANAGEMENT_REJECTED

EXECUTION_SUBMITTED
EXECUTION_CONFIRMED
EXECUTION_FAILED

HEDGE_OPENED
HEDGE_CHANGED
HEDGE_REMOVED

RECONCILIATION_REQUIRED
```

Envelope:

```json
{
  "event_id": "uuid",
  "type": "POSITION_CHANGED",
  "created_at_ms": 0,
  "symbol": "BTC-USDT",
  "correlation_id": "...",
  "causation_id": "...",
  "payload": {}
}
```

---

# 9. Event Store / shared state

Não criar banco novo no V1.

Persistir em:

```text
<strategy_home>/brooks_state/
```

Estrutura:

```text
brooks_state/
├── events.jsonl
│
├── trader/
│   ├── latest.json
│   └── history.jsonl
│
├── htf/
│   ├── latest.json
│   └── history.jsonl
│
└── trades/
    └── <correlation_id>/
        ├── binding.json
        ├── original_trade_intent.json
        ├── latest_management_intent.json
        ├── hedge_state.json
        ├── management_history.jsonl
        └── executions.jsonl
```

`BoundState` do Condor apenas para cursors pequenos:

```text
last_h1_close_ms
last_d1_close_ms
last_pm_timer_ms
last_position_fingerprint
```

Payloads grandes não entram no BoundState.

---

# 10. Scheduler sem drift

Config inicial:

```yaml
brooks:
  trader:
    timeframe: 1h
    wake_offset_sec: 2

  htf:
    timeframe: 1d
    wake_offset_sec: 3

  pm:
    frequency_sec: 60

  position_watcher:
    frequency_sec: 10
```

Não usar:

```python
sleep(3600)
```

como relógio de candle.

Calcular próximo fechamento absoluto.

Exemplo:

```text
14:00:00 close
14:00:02 H1_BAR_CLOSED

15:00:00 close
15:00:02 H1_BAR_CLOSED
```

`decision_time_ms` é o close time da barra.

---

# 11. ClosedBarGate

Portar a semântica do:

```text
brooks-harness/src/market/closed-bar-gate.ts
```

para:

```text
condor/brooks/market_tools.py
```

Regras:

```text
timestamp válido
bar closed
close_time <= decision_time
sem futuro
ordem temporal estrita
sem duplicata
OHLC coerente
gap detectável
quantidade mínima
```

Toda tool de candles Brooks passa pelo gate.

---

# 12. Safe Market Tools

## Trader

```text
get_closed_candles(symbol, timeframe, limit)
get_market_context(symbol)
get_recent_structure(symbol, timeframe)
get_volatility(symbol, timeframe, window)
```

Policy inicial:

```text
allowed TF:
15m
1h
4h
1d

max trader candle limit:
120
```

---

## HTF Analyst

```text
get_closed_candles()
get_recent_structure()
get_volatility()
```

Sem conta.

---

## PM

```text
get_position_state()
get_executor_state()
get_open_orders()
get_recent_fills()

get_original_trade_intent()
get_latest_trader_intent()
get_market_context()

get_recent_structure()
get_volatility()
get_candles(limit <= 30)
```

---

# 13. Tool isolation

O Condor já possui:

- tool profiles;
- per-Agent allowlist;
- mutes;
- MCP registration filtering.

Usar isso.

No modo Brooks, LLMs não montam tools de write:

```text
create_position_executor
create_order_executor
create_grid_executor
create_dca_executor
stop_executor
manage_bots mutation
manage_controllers mutation
```

Somente o host/ExecutionPort chama essas funções depois do GM.

---

# 14. Agent Runner

Criar:

```text
condor/brooks/agent_runner.py
```

Reusar ACP/Pydantic-AI do Condor.

Interface conceitual:

```python
await runner.run(
    role="TRADER",
    model=...,
    skills=[...],
    tools=[...],
    context=...,
    output_model=TradeIntentV2,
    timeout_sec=...,
)
```

Papéis:

```text
TRADER
HTF_ANALYST
POSITION_MANAGER
```

Sem runtime externo novo.

---

# 15. Contratos Pydantic

Arquivo:

```text
condor/brooks/contracts.py
```

Portar do `brooks-harness`/`Skill-Brooks`:

```text
TradeIntentV2
PositionManagementInputV2
ManagementDecisionV2
HedgePlanV2
```

Adicionar envelopes mínimos:

```text
BrooksEventV1
MarketContextV1
GMDecisionV1
ExecutionCommandV1
TradeBindingV1
HedgeStateV1
```

## Management actions V1

Permitir:

```text
HOLD

REDUCE
CLOSE

HEDGE
INCREASE_HEDGE
REDUCE_HEDGE
REMOVE_HEDGE

REQUEST_MARKET_ANALYSIS
RECONCILE_STATE
MANAGEMENT_BLOCKED
```

Deixar para depois, caso não haja primitive segura:

```text
PROTECT
MOVE_PROTECTION
REPLACE_ORDER
```

A posição MAIN já nasce com SL/TP/time_limit via PositionExecutor.

---

# 16. Trader

Evento:

```text
H1_BAR_CLOSED
```

Fluxo:

```text
event
 ↓
latest HTF context
 ↓
Trader agent
 ↓
skills
 ↓
safe market tools
 ↓
TradeIntentV2
 ↓
schema validator
 ↓
semantic validator
 ↓
persist
 ↓
TRADER_INTENT_CREATED
```

Skills:

```text
brooks-market-context
brooks-trade-entry
```

Trader não é bloqueado porque já há posição aberta.

---

# 17. HTF Analyst

Evento:

```text
D1_BAR_CLOSED
```

Skill:

```text
brooks-market-context
```

Output:

```text
MarketContextV1
```

Persistir latest + history.

Publicar:

```text
MARKET_CONTEXT_UPDATED
```

---

# 18. GM entry

Entrada:

```text
TradeIntentV2
```

Se:

```text
NO_TRADE
```

encerrar.

Se:

```text
ENTER_LONG / ENTER_SHORT
```

GM consulta conta e regras.

Sizing inicial para perp linear:

```text
risk_usd = equity * risk_per_trade_pct

stop_distance = abs(entry - invalidation)

base_quantity = risk_usd / stop_distance
```

Depois:

```text
quantize amount
check min size
check min notional
check margin
check max positions
check gross exposure
check leverage
```

Somente então:

```text
ExecutionCommand OPEN_MAIN
```

---

# 19. HummingbotExecutionPort

Arquivo:

```text
condor/brooks/execution.py
```

Interface:

```python
open_main(...)
reduce_main(...)
close_main(...)

open_hedge(...)
increase_hedge(...)
reduce_hedge(...)
remove_hedge(...)

get_state(...)
```

Implementação V1:

```text
open_main
  -> create_position_executor

reduce_main
  -> create_order_executor(position_action=CLOSE)

close_main
  -> prefer controlled close primitive appropriate to binding

open_hedge
  -> create_order_executor(position_action=OPEN)

increase_hedge
  -> create_order_executor(position_action=OPEN)

reduce_hedge
  -> create_order_executor(position_action=CLOSE)

remove_hedge
  -> create_order_executor(position_action=CLOSE)
```

Não alterar Hummingbot API.

---

# 20. Position Watcher

Poll inicial:

```text
10s
```

Ler somente posições/executores ligados ao Brooks run.

Gerar evento somente quando fingerprint mudar.

Fingerprint deve considerar no mínimo:

```text
MAIN qty
HEDGE qty
executor status
position side
open orders status
fills cursor
```

Eventos:

```text
POSITION_OPENED
POSITION_CHANGED
POSITION_CLOSED
FILL
HEDGE_OPENED
HEDGE_CHANGED
HEDGE_REMOVED
```

---

# 21. Position PM

Wakes:

```text
PM_TIMER
POSITION_OPENED
POSITION_CHANGED
FILL
HEDGE_CHANGED
TRADER_INTENT_CREATED
MARKET_CONTEXT_UPDATED
```

Os dois últimos só precisam acordar PM se houver posição ativa.

Contexto inicial:

```text
position
executor
orders
fills

original TraderIntent
latest TraderIntent
latest HTF MarketContext

management history
management policy

hedge state
margin health
```

Skill:

```text
brooks-position-management
```

Output:

```text
ManagementDecisionV2
```

---

# 22. GM management gate

Antes de qualquer write PM:

```text
acquire symbol/correlation lock
re-read venue state
rebuild hedge state
re-check ownership
re-check position quantities
re-check margin
re-check allowed action
re-check target ratio
compile ExecutionCommand
execute
reconcile
release lock
```

Nunca executar baseado apenas no snapshot que o LLM recebeu.

---

# 23. Hedge compiler

Criar:

```text
condor/brooks/hedge.py
```

Responsável por traduzir:

```text
HEDGE
INCREASE_HEDGE
REDUCE_HEDGE
REMOVE_HEDGE
```

para deltas executáveis.

Base:

```text
ratio_basis = absolute_mark_notional
```

Definição:

```text
target_hedge_notional =
    abs(main_mark_notional) * target_hedge_ratio
```

Delta:

```text
delta =
    target_hedge_notional - current_hedge_notional
```

Regras:

```text
HEDGE:
single_main
current ratio == 0
target > 0

INCREASE_HEDGE:
structure ok
target > current

REDUCE_HEDGE:
structure ok
0 <= target < current

REMOVE_HEDGE:
structure ok
target == 0
```

Target ratio:

```text
0 <= ratio <= 1
```

usar decimal canônico.

---

# 24. REQUEST_MARKET_ANALYSIS

Manter a boundary do `brooks-harness`.

PM pode pedir uma análise fresca.

Request contém somente:

```text
symbol
decision time
requested timeframes
requested fields
opaque request id
```

Não pode conter:

```text
side da posição
entry
qty
PnL
account
hedge ratio
ação pretendida
```

Resultado volta como evidência independente.

PM acorda novamente.

---

# 25. Concorrência

Lock por:

```text
account + connector + symbol
```

Uma mutação por vez.

Principalmente evitar:

```text
Trader entry acontecendo
+
PM hedge acontecendo
```

sobre o mesmo instrumento sem serialização.

LLMs podem raciocinar paralelamente.

Writes não.

---

# 26. Features / PRs

Implementar nesta ordem.

---

## FEAT-BROOKS-001 — execution_mode

Entrega:

```text
execution_mode: brooks_agents
```

Requisitos:

- modo `loop` intacto;
- modo novo inicia/encerra;
- nenhum trade.

Commit:

```text
feat(brooks): add isolated brooks_agents execution mode
```

---

## FEAT-BROOKS-002 — contracts

Entrega:

```text
condor/brooks/contracts.py
```

Contratos principais + fixtures.

Commit:

```text
feat(brooks): add trader pm gm and hedge contracts
```

---

## FEAT-BROOKS-003 — event bus + store

Entrega:

```text
events.py
store.py
events.jsonl
latest/history persistence
```

Commit:

```text
feat(brooks): add event bus and durable state store
```

---

## FEAT-BROOKS-004 — closed bar + safe market tools

Entrega:

```text
ClosedBarGate
Trader market tools
PM limited market tools
```

Testar:

```text
forming bar
future bar
duplicates
gap
bad OHLC
PM limit > 30
```

Commit:

```text
feat(brooks): add closed bar gate and safe market tools
```

---

## FEAT-BROOKS-005 — agent runner

Entrega:

```text
role-based run
skills
tool allowlist
structured output
timeout
```

Commit:

```text
feat(brooks): add isolated role agent runner
```

---

## FEAT-BROOKS-006 — Trader

Entrega:

```text
H1 close
→ Trader
→ TradeIntent
```

Shadow mode primeiro.

Commit:

```text
feat(brooks): add independent trader agent
```

---

## FEAT-BROOKS-007 — HTF Analyst

Entrega:

```text
D1 close
→ MarketContext
```

Commit:

```text
feat(brooks): add independent htf analyst
```

---

## FEAT-BROOKS-008 — deterministic GM + MAIN entry

Entrega:

```text
TradeIntent
→ sizing/risk
→ create_position_executor
→ binding persistence
```

Demo only initially.

Commit:

```text
feat(brooks): add deterministic gm and main execution
```

---

## FEAT-BROOKS-009 — position watcher

Entrega:

```text
position/fill/order/hedge events
```

Commit:

```text
feat(brooks): add position and execution watcher
```

---

## FEAT-BROOKS-010 — Position PM

Entrega:

```text
timer/events
→ PM tool loop
→ ManagementDecision
```

Sem writes ainda.

Commit:

```text
feat(brooks): add independent position manager agent
```

---

## FEAT-BROOKS-011 — PM HOLD/REDUCE/CLOSE

Entrega:

```text
GM management gate
ExecutionPort management writes
```

Commit:

```text
feat(brooks): execute pm reduce and close actions
```

---

## FEAT-BROOKS-012 — Hedge Mode + hedge compiler

Entrega:

```text
ensure HEDGE position mode
HedgeState
ownership
ratio compiler
```

Sem write de hedge até os testes passarem.

Commit:

```text
feat(brooks): add hedge state and deterministic hedge compiler
```

---

## FEAT-BROOKS-013 — hedge execution

Entrega:

```text
HEDGE
INCREASE_HEDGE
REDUCE_HEDGE
REMOVE_HEDGE
```

via:

```text
create_order_executor
position_action OPEN/CLOSE
```

Testnet/demo obrigatório.

Commit:

```text
feat(brooks): execute hedge lifecycle through hummingbot
```

---

## FEAT-BROOKS-014 — fresh market analysis request

Entrega:

```text
REQUEST_MARKET_ANALYSIS
```

com privacy boundary.

Commit:

```text
feat(brooks): add pm fresh market analysis workflow
```

---

## FEAT-BROOKS-015 — replay/evidence

Entrega:

```text
replay correlation_id
```

Mostrar:

```text
Trader
HTF
GM
execution
PM
hedge changes
fills
final state
```

Commit:

```text
feat(brooks): add deterministic trade lifecycle replay
```

---

# 27. Test strategy

Cada feature precisa de:

```text
unit
contract
adversarial quando aplicável
```

Não exigir live exchange para unit.

Usar fakes para:

```text
HummingbotExecutionPort
MarketData
Portfolio
Position state
```

Integração real somente em demo/testnet.

---

# 28. Hedge adversarial tests obrigatórios

Casos:

```text
duplicate MAIN
duplicate HEDGE
orphan HEDGE
unknown ownership
same-side MAIN/HEDGE
ratio < 0
ratio > 1
REMOVE with target != 0
INCREASE with target <= current
REDUCE with target >= current
HEDGE when hedge already exists
stale position snapshot
position disappears before write
partial fill
order failure
ambiguous execution
restart with open MAIN + HEDGE
```

Fail closed.

---

# 29. Commit / push policy

O agente implementador deve trabalhar em **feature commits pequenos**.

Antes de alterar código:

```bash
git status
git remote -v
git branch --show-current
git log -5 --oneline
```

Identificar:

- upstream oficial;
- fork pessoal gravável.

Regra:

```text
NUNCA push para hummingbot/condor.
```

Criar branch de trabalho no fork pessoal:

```text
feat/brooks-agents-v1
```

ou equivalente se já existir convenção no repo.

Após cada feature:

```text
run targeted tests
run relevant existing tests
git diff review
git add scoped files
git commit
git push personal_remote branch
```

Se não existir remote pessoal gravável:

- continuar fazendo commits locais;
- não criar push para upstream;
- reportar exatamente qual remote falta.

Não misturar múltiplas features grandes num único commit.

---

# 30. Subagents

O Codex principal é o **Integrator**.

Ele deve delegar tarefas independentes a subagents para ganhar velocidade.

Sugestão de divisão:

```text
SUBAGENT A
Condor runtime audit + execution_mode + event lifecycle

SUBAGENT B
contracts + fixtures + ClosedBarGate

SUBAGENT C
Trader + HTF + role tool isolation

SUBAGENT D
GM + HummingbotExecutionPort + sizing

SUBAGENT E
PositionWatcher + PM input/tooling

SUBAGENT F
hedge state/compiler/execution

SUBAGENT G
tests/adversarial/replay
```

Regras:

- cada subagent recebe escopo de arquivos claro;
- evitar dois subagents editarem o mesmo arquivo;
- subagent deve devolver findings + diff/commit;
- Integrator revisa e integra;
- Integrator roda testes;
- Integrator é responsável pelo push do branch principal;
- usar worktrees/branches isoladas se a infraestrutura suportar;
- não delegar decisões arquiteturais centrais sem revisar.

---

# 31. Fast-path milestones

## MILESTONE A

Após FEAT-006:

```text
H1
→ Trader
→ tools
→ TradeIntent
```

Rodar shadow continuamente.

---

## MILESTONE B

Após FEAT-008:

```text
Trader
→ GM
→ Hummingbot
→ MAIN position
```

Demo/testnet.

A MAIN já nasce com:

```text
SL
TP
time limit
```

---

## MILESTONE C

Após FEAT-011:

```text
Trader
→ GM
→ MAIN
→ PositionWatcher
→ PM
→ HOLD/REDUCE/CLOSE
```

---

## MILESTONE D

Após FEAT-013:

```text
MAIN
+
PM dynamic hedge
```

com:

```text
HEDGE
INCREASE
REDUCE
REMOVE
```

---

# 32. Definition of Done V1

V1 está pronto quando:

1. `execution_mode=loop` continua funcionando;
2. `brooks_agents` inicia e recupera estado;
3. H1 acorda Trader uma vez;
4. D1 acorda HTF Analyst uma vez;
5. Trader não vê conta;
6. Trader não possui write tools;
7. Trader usa skills/tools;
8. Trader produz `TradeIntentV2`;
9. GM faz sizing determinístico;
10. MAIN abre via Hummingbot;
11. tese original fica persistida;
12. Trader continua analisando com MAIN aberta;
13. PositionWatcher detecta mudanças;
14. PM acorda por timer e eventos;
15. PM recebe original + latest TraderIntent + HTF;
16. PM pode consultar até 30 candles sob demanda;
17. PM produz `ManagementDecisionV2`;
18. GM revalida estado antes de write;
19. PM consegue REDUCE/CLOSE;
20. account está em HEDGE mode quando necessário;
21. ownership MAIN/HEDGE é explícito;
22. PM consegue HEDGE;
23. PM consegue INCREASE_HEDGE;
24. PM consegue REDUCE_HEDGE;
25. PM consegue REMOVE_HEDGE;
26. restart reconcilia MAIN/HEDGE;
27. todos os eventos ficam auditáveis;
28. replay reconstrói a operação;
29. cada feature está em commit próprio;
30. branch está sendo pushado somente para o fork pessoal.

---

# 33. Não fazer no V1

Não fazer:

```text
novo framework
novo banco
Redis
Kafka
LangGraph
debate multi-agent
RL
RAG novo
dashboard novo
Telegram novo
10 exchanges simultâneas
portfolio multi-strategy complexo
hedge > 100%
auto leverage pelo LLM
LLM execution tools
LLM position sizing
dynamic stop hack
```

Também não mexer na Hummingbot API apenas porque uma ação parece inconveniente.

Primeiro usar as primitives existentes corretamente.

---

# 34. Próximo passo depois do V1

Depois do fluxo estável:

```text
PROTECT
MOVE_PROTECTION
```

somente após confirmar uma primitive Hummingbot segura para proteção dinâmica.

Depois:

```text
multiple symbols
multiple strategies
direct Binance adapter
direct Hyperliquid adapter
additional HTF analysts
forecast model tools
```

Mas o núcleo não muda:

```text
market event
→ agent reasoning
→ structured intent
→ deterministic GM
→ execution
→ position event
→ PM reasoning
→ structured management
→ deterministic GM
```

---

# 35. Resultado esperado

```text
CONDOR
│
├── legacy mode
│   └── execution_mode=loop
│
└── Brooks mode
    └── execution_mode=brooks_agents
        │
        ├── Trader Agent
        ├── HTF Analyst
        ├── Position PM Agent
        │
        ├── Event Bus
        ├── Shared/Event State
        │
        ├── deterministic GM
        ├── hedge compiler
        │
        └── HummingbotExecutionPort
             │
             ▼
        Hummingbot API
             │
             ▼
           Venue
```

A regra central é:

```text
LLM = pensar
Skills = conhecimento
Tools = investigar
Contracts = comunicar
GM = autorizar/compilar
Hummingbot = executar
Event Store = auditar
```

Esse é o V1.
