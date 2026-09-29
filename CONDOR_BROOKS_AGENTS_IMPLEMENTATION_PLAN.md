# Condor Brooks Agents — Plano de Implementação Rápida

**Objetivo:** manter o **Condor como harness/runtime** e trazer para dentro dele a lógica útil do `brooks-harness` e do `Skill-Brooks`, sem transformar o projeto em outro framework.

O resultado desejado é simples:

- Trader LLM acorda quando houver novo evento de mercado.
- HTF Analyst LLM atualiza contexto de prazo maior em outra cadência.
- Position PM LLM acorda por timer e por mudança de posição.
- Cada agente lê skills e usa apenas as ferramentas permitidas para o seu papel.
- Cada agente produz um output estruturado.
- Um GM determinístico valida risco, sizing e ações antes de qualquer mutação.
- A execução real continua usando a infraestrutura do Condor/Hummingbot.
- O desenho fica preparado para adapters diretos no futuro, mas **o MVP usa Hummingbot**.

---

# 1. Arquitetura alvo

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
         ┌───────────┼───────────┐
         ▼           ▼           ▼
    Hummingbot    Binance    Hyperliquid
       MVP        futuro       futuro
```

No primeiro release:

```text
EXECUTION PORT
      │
      ▼
HummingbotExecutionPort
      │
      ▼
Hummingbot API
```

Não implementar Binance/Hyperliquid direto no MVP.

A abstração existe apenas para não acoplar `TraderIntent` e `ManagementIntent` ao formato interno da Hummingbot API.

---

# 2. Princípios que não devem ser quebrados

## 2.1 Condor continua sendo o runtime

Não criar outro harness.

Usar do Condor:

- lifecycle;
- start / stop / pause;
- `LoopSupervisor`;
- restart;
- `agent_key`;
- ACP/Pydantic-AI;
- MCP;
- skills;
- journal;
- tool allowlist/mutes;
- servidor Hummingbot já configurado;
- executores;
- portfolio;
- risk infrastructure;
- session/reporting.

A nova arquitetura entra como um modo novo:

```yaml
execution_mode: brooks_agents
```

O modo atual continua:

```yaml
execution_mode: loop
```

`loop` não muda de comportamento.

---

## 2.2 Trader não executa

O Trader responde apenas:

```text
ENTER_LONG
ENTER_SHORT
NO_TRADE
```

usando o contrato já existente no `Skill-Brooks`:

```text
brooks.trade-intent.v2
```

Não criar um contrato novo sem necessidade.

O Trader não recebe:

- saldo;
- equity;
- margem;
- posições;
- PnL;
- executores;
- ordens;
- fills;
- exposição da conta.

O Trader não recebe tools de:

- criar executor;
- parar executor;
- enviar ordem;
- cancelar ordem;
- alterar posição;
- gerir bot.

O Trader pode continuar opinando mesmo com posição aberta.

Exemplo:

```text
10:00 ENTER_LONG
11:00 ENTER_LONG
12:00 NO_TRADE
13:00 ENTER_SHORT
```

Essa evolução da leitura é importante para o PM.

---

## 2.3 PM não é um segundo Trader

O PM recebe primeiro o estado operacional da posição e as conclusões já produzidas pelos analistas.

Ele **não recebe um bloco enorme de candles no contexto inicial**.

Contexto inicial do PM:

```text
posição atual
executor
ordens
fills
PnL
proteção
custos
tese original do Trader
última intenção do Trader
último MarketContext HTF
histórico de gestão
policy
```

Mas o PM pode investigar o mercado usando ferramentas limitadas.

Ferramentas propostas:

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

A ferramenta `get_candles` do PM:

- somente candles fechadas;
- sempre passa pelo `ClosedBarGate`;
- limite rígido <= 30;
- sem acesso arbitrário a conta ou execução;
- não aceita timestamp futuro;
- não aceita `limit` maior que a policy;
- registra a chamada no Event Store.

Assim o PM continua agentic sem receber um dump de mercado automaticamente.

---

## 2.4 HTF Analyst é independente

O HTF Analyst não abre trade.

Ele produz:

```text
MarketContext
```

Exemplo:

```json
{
  "schema": "brooks.market-context.v1",
  "symbol": "BTC-USDT",
  "as_of_ms": 0,
  "primary_regime": "bull-trend",
  "phase": "channel",
  "directional_pressure": "bull",
  "breakout_mode": false,
  "always_in": "long",
  "confidence": "medium",
  "key_levels": [],
  "transition_conditions": []
}
```

No primeiro release ele acorda no fechamento D1.

Depois poderemos ter:

```text
H4 analyst
forecast analyst
volatility analyst
news analyst
```

Todos publicando contexto, sem mexer no Trader/PM.

---

# 3. Onde isso entra no Condor

Criar uma área nova:

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
    ├── execution.py
    ├── position_watcher.py
    └── supervisor.py
```

Novo Agent Condor:

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

Não modificar agentes existentes.

---

# 4. Integração com TickEngine

O `TickEngine` atual é dono do lifecycle.

Não transformar o `_tick()` existente em:

```text
PM -> Trader -> GM
```

Isso manteria tudo preso ao mesmo relógio.

O modo novo deve ser bifurcado antes do loop tradicional.

Conceitualmente:

```python
if execution_mode == "brooks_agents":
    await BrooksSupervisor(...).run()
    return
```

O `BrooksSupervisor` usa o mesmo ciclo de vida da sessão Condor.

Ele cria tarefas independentes:

```text
BrooksSupervisor
   ├── MarketClock
   ├── TraderConsumer
   ├── HTFConsumer
   ├── PMTimer
   ├── PositionWatcher
   ├── PMConsumer
   └── GMConsumer
```

No stop:

```text
cancel tasks
flush state
flush event store
finalize journal
```

O restante fica com o lifecycle já existente do Condor.

---

# 5. Event Bus

Criar:

```text
condor/brooks/events.py
```

MVP em memória:

```python
asyncio.Queue
```

Não adicionar Kafka, Redis, NATS ou RabbitMQ.

Cada evento também é persistido no Event Store.

Eventos iniciais:

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
```

Formato:

```json
{
  "event_id": "uuid",
  "type": "H1_BAR_CLOSED",
  "created_at_ms": 0,
  "symbol": "BTC-USDT",
  "payload": {},
  "correlation_id": null,
  "causation_id": null
}
```

`correlation_id` acompanha um trade.

Exemplo:

```text
TraderIntent
     │
     ▼
GM
     │
     ▼
Position
     │
     ▼
PM
```

todos usam o mesmo `correlation_id`.

---

# 6. Event Store

Criar:

```text
condor/brooks/store.py
```

Não adicionar banco novo no MVP.

Persistência:

```text
<strategy_home>/
└── brooks_state/
    ├── events.jsonl
    ├── trader/
    │   ├── latest.json
    │   └── history.jsonl
    ├── contexts/
    │   ├── latest_daily.json
    │   └── history.jsonl
    └── positions/
        └── <correlation_id>/
            ├── binding.json
            ├── original_trade_intent.json
            ├── latest_pm_intent.json
            └── management_history.jsonl
```

Usar escrita atômica para arquivos substituíveis:

```text
write tmp
close
rename
```

JSONL para histórico.

O `BoundState` do Condor fica apenas para cursors pequenos:

```text
last_h1_close_ms
last_d1_close_ms
last_pm_timer_ms
```

Não guardar payload grande no `BoundState`.

---

# 7. Scheduler / Market Clock

Criar:

```text
condor/brooks/supervisor.py
```

e helpers em:

```text
condor/brooks/events.py
```

Config:

```yaml
brooks:
  trader:
    timeframe: "1h"
    wake_offset_sec: 2

  htf:
    timeframe: "1d"
    wake_offset_sec: 3

  pm:
    frequency_sec: 60

  position_watcher:
    frequency_sec: 10
```

Não usar simplesmente:

```python
await sleep(3600)
```

porque gera drift.

Calcular sempre o próximo fechamento absoluto da barra.

Exemplo:

```text
14:00:00 H1 fecha
14:00:02 H1_BAR_CLOSED

15:00:00 H1 fecha
15:00:02 H1_BAR_CLOSED
```

O evento carrega:

```text
decision_time_ms = close_time_ms
```

e não o horário em que o LLM terminou.

---

# 8. ClosedBarGate

Portar do `brooks-harness` a semântica de:

```text
src/market/closed-bar-gate.ts
```

para:

```text
condor/brooks/market_tools.py
```

Toda ferramenta que entrega candles aos agentes passa por esse gate.

Regras mínimas:

- timestamps válidos;
- `close_time <= decision_time`;
- nenhuma barra futura;
- nenhuma barra forming;
- ordem temporal estrita;
- sem duplicatas;
- gap detectável;
- OHLC coerente;
- quantidade mínima;
- trigger timeframe alinhado ao decision time quando aplicável.

Nada de candle bruto direto vindo da Hummingbot API para o LLM sem essa camada.

---

# 9. Ferramentas seguras de mercado

Não expor o cliente Hummingbot bruto para Trader/PM.

Criar tool facade Brooks.

Arquivo:

```text
condor/brooks/market_tools.py
```

## 9.1 Trader tools

```text
get_closed_candles(symbol, timeframe, limit)
get_market_context(symbol)
get_recent_structure(symbol, timeframe)
get_volatility(symbol, timeframe, window)
```

Policy:

```text
get_closed_candles:
  allowed_timeframes: [15m, 1h, 4h, 1d]
  max_limit: 120
```

O Trader pode investigar.

Ele não recebe automaticamente todas as 120x4 barras.

Prompt inicial:

```text
evento H1_BAR_CLOSED
symbol
decision_time
último MarketContext HTF
```

O LLM decide quais tools usar.

Isso aproxima o comportamento de um coding agent:

```text
acorda
↓
lê skill
↓
consulta tools
↓
raciocina
↓
consulta outra tool
↓
emite TradeIntent
```

## 9.2 HTF Analyst tools

```text
get_closed_candles(symbol, 1d, ...)
get_closed_candles(symbol, 4h, ...)
get_recent_structure(...)
get_volatility(...)
```

Sem conta e sem execução.

## 9.3 PM tools

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

PM `get_candles` é deliberadamente menor.

O PM pode olhar preço se concluir que isso é necessário, mas não vira um Trader com 120 barras de quatro timeframes por padrão.

---

# 10. Tool boundaries no Condor

O Condor já possui allowlist/mutes reais por Agent.

Usar isso em vez de inventar outro sistema.

Para `brooks_price_action`, a sessão não deve montar automaticamente as tools de mutação quando o papel ativo for Trader/HTF/PM reasoning.

As mutações ficam no host:

```text
ManagementIntent
      │
      ▼
GM
      │
      ▼
ExecutionPort
```

O LLM nunca chama diretamente:

```text
create_position_executor
create_order_executor
stop_executor
manage_bots
manage_controllers
```

no modo Brooks.

Isso vale inclusive para o PM.

---

# 11. Agent Runner

Criar:

```text
condor/brooks/agent_runner.py
```

Objetivo:

usar o runtime nativo do Condor para executar um turno isolado com:

```text
role
agent_key/model
skills
tool allowlist
initial context
expected schema
timeout
```

Interface conceitual:

```python
await runner.run(
    role="TRADER",
    model=...,
    skills=[...],
    tools=[...],
    context=...,
    output_model=TradeIntent,
)
```

Papéis:

```text
TRADER
HTF_ANALYST
POSITION_MANAGER
```

Não criar subprocess executor novo.

Reusar ACP/Pydantic AI que o Condor já usa.

---

# 12. Contratos

Criar:

```text
condor/brooks/contracts.py
```

Portar os contratos atuais em Pydantic.

## 12.1 TraderIntent

Manter:

```text
brooks.trade-intent.v2
```

Decisões:

```text
ENTER_LONG
ENTER_SHORT
NO_TRADE
```

Campos principais:

```text
symbol
decision_time_ms
market_context
setup
decision_timeframe
context_timeframes_used
entry_mechanism
trigger
invalidation
evidence_for
evidence_against
qualitative_confidence
uncertainty
conditions_that_change_market_read
```

## 12.2 MarketContext

Novo envelope Condor simples:

```text
brooks.market-context.v1
```

Não precisa replicar uma árvore enorme.

## 12.3 PositionManagementInput

Portar:

```text
brooks.position-management-input.v2
```

## 12.4 ManagementDecision

Portar:

```text
brooks.management-decision.v2
```

## 12.5 GMDecision

Novo contrato determinístico:

```json
{
  "schema": "condor.brooks.gm-decision.v1",
  "kind": "ENTRY",
  "approved": true,
  "reason": "OK",
  "correlation_id": "...",
  "execution": {}
}
```

O GM não precisa de LLM.

---

# 13. Trader Agent

Arquivo:

```text
condor/brooks/trader.py
```

Evento:

```text
H1_BAR_CLOSED
```

Fluxo:

```text
H1_BAR_CLOSED
      │
      ▼
load latest HTF context
      │
      ▼
start Trader agent
      │
      ├── read brooks-market-context
      ├── read brooks-trade-entry
      ├── call safe market tools
      └── reason
      │
      ▼
TradeIntent
      │
      ▼
validate
      │
      ▼
STATE STORE
      │
      ▼
TRADER_INTENT_CREATED
```

Validar também referências estruturais:

se `trigger.price` afirmar vir de uma barra consultada, a referência deve existir no material retornado pelas tools durante aquele run.

Se o output for inválido:

```text
fail closed
no GM event
```

---

# 14. HTF Analyst

Arquivo:

```text
condor/brooks/htf_analyst.py
```

Evento:

```text
D1_BAR_CLOSED
```

Skills:

```text
brooks-market-context
```

Output:

```text
MarketContext
```

Persistir como:

```text
latest_daily.json
```

Publicar:

```text
MARKET_CONTEXT_UPDATED
```

O Trader H1 seguinte recebe o contexto novo.

O PM também consegue consultar:

```text
get_market_context()
```

---

# 15. GM determinístico

Arquivo:

```text
condor/brooks/gm.py
```

O GM recebe dois tipos de decisão:

```text
TraderIntent
ManagementIntent
```

e decide se podem virar mutação.

## 15.1 Para TraderIntent

GM consulta:

- account;
- balance/equity;
- margem;
- posições;
- executores;
- risk limits;
- trading rules.

Se:

```text
NO_TRADE
```

nenhuma execução.

Se:

```text
ENTER_LONG / ENTER_SHORT
```

calcula sizing.

Para perp linear inicialmente:

```text
risk_usd = equity * risk_per_trade_pct

stop_distance = abs(entry - invalidation)

quantity = risk_usd / stop_distance
```

Depois:

```text
quantize
min amount
min notional
margin
leverage
exposure
max positions
```

Somente então cria execution command.

O cálculo precisa ser isolado por tipo de mercado.

Não assumir que a fórmula linear serve para todos os connectors.

MVP:

```text
1 connector perp demo
1 symbol
```

depois generalizar.

## 15.2 Para ManagementIntent

GM verifica:

- posição ainda existe?
- snapshot ainda é atual?
- quantidade ainda bate?
- ação é permitida?
- ação reduz/aumenta risco?
- há mutação simultânea?
- existe primitive Hummingbot segura?

Se não:

```text
GM_MANAGEMENT_REJECTED
```

---

# 16. Execution Port

Criar:

```text
condor/brooks/execution.py
```

Interface mínima:

```python
class ExecutionPort:
    async def open_position(...)
    async def reduce_position(...)
    async def close_position(...)
    async def get_execution_state(...)
```

Primeira implementação:

```text
HummingbotExecutionPort
```

Mapeamento:

```text
open_position
  -> create_position_executor

reduce_position
  -> create_order_executor

close_position
  -> stop_executor(keep_position=False)
```

No futuro:

```text
BinanceExecutionPort
HyperliquidExecutionPort
```

sem alterar Trader, HTF Analyst ou PM.

Isso não significa implementar adapters agora.

---

# 17. Position Watcher

Criar:

```text
condor/brooks/position_watcher.py
```

A cada:

```yaml
frequency_sec: 10
```

ler apenas posições/executores Brooks.

Identificação via:

```text
controller_id / correlation_id
```

Gerar eventos quando fingerprint mudar:

```text
POSITION_OPENED
POSITION_CHANGED
POSITION_CLOSED
ORDER_CHANGED
FILL
```

Fingerprint simples:

```text
position qty
mark/entry relevant state
executor status
open order ids/status
```

Não emitir `POSITION_CHANGED` em todo poll se nada mudou.

---

# 18. Position PM Agent

Arquivo:

```text
condor/brooks/pm.py
```

Wakes:

```text
PM_TIMER
POSITION_OPENED
POSITION_CHANGED
FILL
ORDER_CHANGED
MARKET_CONTEXT_UPDATED   # somente se existir posição ativa
TRADER_INTENT_CREATED    # somente se existir posição ativa
```

Isso é melhor que depender apenas de timer.

O timer continua como reconciliação periódica.

## Contexto inicial

```text
position snapshot
executor snapshot
open orders
recent fills

original TradeIntent
latest TradeIntent
latest HTF MarketContext

management history
management policy
```

## Tools

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

## Skills

```text
brooks-position-management
```

## Output

```text
ManagementDecision v2
```

Persistir e emitir:

```text
MANAGEMENT_INTENT_CREATED
```

---

# 19. Ações do PM no primeiro release

Não habilitar o contrato inteiro no primeiro dia.

Policy inicial:

```text
HOLD
REDUCE
CLOSE
REQUEST_MARKET_ANALYSIS
RECONCILE_STATE
MANAGEMENT_BLOCKED
```

Deixar bloqueadas até termos primitive segura confirmada:

```text
PROTECT
MOVE_PROTECTION
HEDGE
INCREASE_HEDGE
REDUCE_HEDGE
REMOVE_HEDGE
REPLACE_ORDER
```

O LLM recebe em `management_policy` exatamente a lista permitida.

Isso evita que ele gere uma ação que o backend não consegue executar com segurança.

---

# 20. REQUEST_MARKET_ANALYSIS

Manter a boa separação já existente no `brooks-harness`.

Se o PM precisar de leitura nova de mercado:

```text
PM
 │
 └── REQUEST_MARKET_ANALYSIS
           │
           ▼
market-only analysis run
           │
           ▼
fresh market analysis
           │
           ▼
PM wakes again
```

A request nunca contém:

- lado da posição;
- entrada;
- quantidade;
- PnL;
- saldo;
- hedge;
- ação que o PM quer justificar.

Assim o Trader/analista não é contaminado pela posição.

---

# 21. Gestão de stop

No código atual do Condor inspecionado existem caminhos claros para:

```text
create_position_executor
create_order_executor
stop_executor
```

Não assumir que existe uma primitive genérica confiável para atualizar stop de um `PositionExecutor` vivo.

Portanto:

## MVP

A posição já nasce com:

```text
stop_loss
take_profit
time_limit
```

via `create_position_executor`.

PM pode:

```text
HOLD
REDUCE
CLOSE
```

## Feature posterior

Investigar a Hummingbot API instalada e implementar:

```text
PROTECT
MOVE_PROTECTION
```

somente com primitive oficial/testada.

Não fazer workaround perigoso de:

```text
stop executor
recriar executor
```

só para mover stop.

---

# 22. Concorrência

Criar lock por símbolo/correlation id.

```python
locks[symbol] = asyncio.Lock()
```

Toda mutação:

```text
GM entry
PM reduce
PM close
```

passa pelo mesmo lock.

Fluxo:

```text
intent
  │
acquire lock
  │
re-read current state
  │
validate again
  │
execute
  │
reconcile
  │
release
```

O LLM pode ter raciocinado sobre um snapshot de 5 segundos atrás.

Por isso o GM sempre faz revalidação imediatamente antes da escrita.

---

# 23. Feature plan

A implementação deve ser feita nesta ordem.

---

## FEAT-BROOKS-001 — Isolated execution mode

### Entrega

```text
execution_mode: brooks_agents
```

wiring mínimo no lifecycle.

### Não faz

- LLM;
- trade;
- event bus real.

### Aceite

- `loop` continua byte-for-byte semanticamente igual;
- suíte atual verde;
- novo modo sobe e encerra sem erro.

---

## FEAT-BROOKS-002 — Brooks contracts

### Entrega

```text
condor/brooks/contracts.py
```

com:

```text
BrooksEvent
TradeIntentV2
MarketContextV1
PositionManagementInputV2
ManagementDecisionV2
GMDecisionV1
```

### Aceite

Fixtures do `Skill-Brooks`/`brooks-harness` compatíveis validam.

---

## FEAT-BROOKS-003 — Event Bus + Event Store

### Entrega

```text
BrooksEventBus
BrooksStore
events.jsonl
```

### Aceite

```text
publish
consume
persist
restart cursor
```

testados.

---

## FEAT-BROOKS-004 — ClosedBarGate + safe market tools

### Entrega

```text
get_closed_candles
get_recent_structure
get_volatility
get_market_context
```

### Aceite

Rejeitar:

- forming;
- future;
- duplicate;
- gap;
- OHLC inválido.

PM não consegue pedir `limit > 30`.

Trader não consegue pedir acima da policy.

---

## FEAT-BROOKS-005 — Agent runner

### Entrega

Runner reutilizando o runtime Condor.

Suporta:

```text
role
skills
tools
model
context
structured output
timeout
```

### Aceite

Fake agent + 1 modelo real retornando Pydantic output.

---

## FEAT-BROOKS-006 — Trader Agent

### Entrega

```text
H1_BAR_CLOSED
→ Trader
→ tools
→ skills
→ TradeIntent
→ store
→ TRADER_INTENT_CREATED
```

### Aceite

Trader não possui nenhuma tool de mutação.

### Milestone

Aqui já podemos fazer shadow test contínuo do Trader.

---

## FEAT-BROOKS-007 — HTF Analyst

### Entrega

```text
D1_BAR_CLOSED
→ HTF analyst
→ MarketContext
→ store
→ MARKET_CONTEXT_UPDATED
```

### Aceite

Trader seguinte consegue ler o contexto D1.

---

## FEAT-BROOKS-008 — Deterministic GM + entry

### Entrega

```text
TradeIntent
→ GM
→ risk
→ sizing
→ HummingbotExecutionPort
→ create_position_executor
```

Persistir:

```text
correlation_id
controller_id
executor_id
original TradeIntent
GM decision
```

### Aceite

No ambiente demo:

```text
valid ENTER
→ executor criado
→ posição rastreável
```

### Milestone

Primeiro sistema capaz de abrir posições de forma autônoma.

---

## FEAT-BROOKS-009 — Position Watcher

### Entrega

Detectar:

```text
POSITION_OPENED
POSITION_CHANGED
POSITION_CLOSED
ORDER_CHANGED
FILL
```

### Aceite

Sem spam de evento quando o estado não mudou.

---

## FEAT-BROOKS-010 — Position PM Agent

### Entrega

```text
timer / position event / trader intent
→ PM
→ skills
→ tools
→ ManagementDecision
→ MANAGEMENT_INTENT_CREATED
```

### Aceite

PM recebe:

```text
original intent
latest intent
HTF context
position
orders
fills
```

e pode chamar `get_candles(limit<=30)`.

---

## FEAT-BROOKS-011 — Management execution

### Entrega

Suportar inicialmente:

```text
HOLD
REDUCE
CLOSE
RECONCILE_STATE
MANAGEMENT_BLOCKED
```

### Aceite

Demo:

```text
REDUCE
→ partial close
→ position watcher sees new qty

CLOSE
→ executor stopped/position closed
→ watcher confirms
```

### Milestone

Fluxo completo:

```text
market
→ Trader
→ GM
→ position
→ PM
→ management
```

---

## FEAT-BROOKS-012 — PM fresh market analysis

### Entrega

```text
REQUEST_MARKET_ANALYSIS
```

com boundary market-only.

### Aceite

PM request não pode vazar estado privado para o market analyst.

---

## FEAT-BROOKS-013 — Dynamic protection

Somente após verificar primitive Hummingbot segura.

### Entrega possível

```text
PROTECT
MOVE_PROTECTION
```

### Gate

Não implementar se exigir hack de lifecycle de executor.

---

## FEAT-BROOKS-014 — Replay / evidence

### Entrega

CLI:

```text
condor brooks replay <correlation_id>
```

Mostrar:

```text
events
TraderIntent
MarketContext
GM decision
execution
PM decisions
fills
final position result
```

Não chamar LLM no replay padrão.

---

# 24. Configuração inicial

Exemplo:

```yaml
execution_mode: brooks_agents

connector_name: binance_perpetual_demo
trading_pair: BTC-USDT

brooks:
  trader:
    timeframe: "1h"
    wake_offset_sec: 2
    model:
      agent_key: null

    candle_limits:
      1h: 120
      4h: 120
      1d: 120

  htf:
    enabled: true
    timeframe: "1d"
    wake_offset_sec: 3

  pm:
    frequency_sec: 60
    max_candle_limit: 30

  position_watcher:
    frequency_sec: 10

  gm:
    max_open_positions: 1
    risk_per_trade_pct: 0.005
    leverage: 1

  management:
    allowed_actions:
      - HOLD
      - REDUCE
      - CLOSE
      - REQUEST_MARKET_ANALYSIS
      - RECONCILE_STATE
      - MANAGEMENT_BLOCKED
```

Começar conservador no demo.

Os valores de risco do live não devem ser hardcoded no código.

---

# 25. Fluxo completo de exemplo

## 14:00 — H1 fecha

```text
MarketClock
   │
   ▼
H1_BAR_CLOSED
```

Trader recebe:

```text
symbol
decision time
latest D1 MarketContext
```

Trader:

```text
load brooks-market-context
load brooks-trade-entry

get_closed_candles(H1, 120)
get_closed_candles(H4, 120)
get_recent_structure(...)
```

retorna:

```text
ENTER_LONG
```

Store:

```text
trader/latest.json
trader/history.jsonl
```

Event:

```text
TRADER_INTENT_CREATED
```

---

## GM recebe intenção

Consulta:

```text
account
positions
executors
trading rules
```

Calcula sizing.

Aprova.

Emite:

```text
ExecutionCommand.OPEN
```

`HummingbotExecutionPort` chama:

```text
create_position_executor
```

com:

```text
SL
TP
time_limit
controller_id
amount
```

Store salva:

```text
original_trade_intent.json
binding.json
```

---

## Position Watcher detecta posição

```text
POSITION_OPENED
```

PM acorda imediatamente.

Contexto:

```text
posição
executor
ordens
original intent
latest intent
latest D1 context
```

PM talvez faça:

```text
get_volatility()
get_recent_structure()
```

retorna:

```text
HOLD
```

Nada executado.

---

## 14:30 — PM timer

```text
PM_TIMER
```

PM vê posição atual.

Talvez consulte:

```text
get_candles(BTC-USDT, 15m, 20)
```

retorna:

```text
HOLD
```

---

## 15:00 — novo H1

Trader acorda independentemente.

Retorna:

```text
NO_TRADE
```

`latest_trade_intent` muda.

Como há posição ativa:

```text
TRADER_INTENT_CREATED
```

também acorda PM.

PM recebe:

```text
original = ENTER_LONG
latest = NO_TRADE
position still LONG
```

Ele raciocina sobre isso.

Pode:

```text
HOLD
REDUCE
CLOSE
```

conforme skill + estado.

---

## Dia seguinte — D1 fecha

HTF Analyst produz:

```text
MarketContext = transition / bear pressure
```

`MARKET_CONTEXT_UPDATED`.

Como há posição ativa, PM pode acordar e reavaliar.

---

# 26. O que NÃO fazer

Não fazer agora:

- outro harness;
- outro serviço;
- outro banco;
- multi-agent debate;
- Trader conversando diretamente com PM;
- PM executando tools de write;
- LLM fazendo position sizing;
- LLM decidindo limite global da conta;
- LLM controlando leverage livremente;
- candles forming;
- Hummingbot API fork;
- suporte simultâneo a dez exchanges;
- hedge no primeiro MVP;
- RL;
- memória vetorial;
- RAG novo;
- dashboard novo;
- Telegram novo;
- backtester novo.

Primeiro fazer:

```text
Trader
GM
Position PM
```

funcionarem corretamente no Condor.

---

# 27. Arquivos existentes que devem ser reaproveitados como referência

## Condor

Principais:

```text
condor/agents/engine.py
condor/agents/prompts.py
condor/agents/risk.py
condor/runtime/loops.py
condor/runtime/toolsets.py
condor/runtime/state.py
condor/memory/skills.py

condor/fetchers/market_data.py
condor/fetchers/positions.py
condor/fetchers/executors.py

mcp_servers/hummingbot_api/tools/market_data.py
mcp_servers/hummingbot_api/tools/portfolio.py
mcp_servers/hummingbot_api/tools/trading.py
mcp_servers/hummingbot_api/tools/executor_create.py
mcp_servers/hummingbot_api/tools/executors.py
```

## brooks-harness

Portar ideias e contratos, não o runtime:

```text
src/contracts/trade-intent.ts
src/contracts/position-management.ts
src/contracts/management-intent.ts
src/market/closed-bar-gate.ts
src/pm/market-analysis-boundary.ts
src/trader/validator.ts
src/pm/validator.ts
```

## Skill-Brooks

Usar diretamente como inteligência:

```text
brooks-market-context
brooks-trade-entry
brooks-position-management
```

---

# 28. Sequência prática de PRs

Não fazer uma mega-PR.

```text
PR-01  execution_mode brooks_agents
PR-02  contracts
PR-03  event bus + store
PR-04  ClosedBarGate + safe market tools
PR-05  agent runner
PR-06  Trader
PR-07  HTF Analyst
PR-08  GM + Hummingbot entry
PR-09  position watcher
PR-10  Position PM
PR-11  PM execution HOLD/REDUCE/CLOSE
PR-12  REQUEST_MARKET_ANALYSIS
PR-13  replay/evidence
PR-14  dynamic stop/protection
```

Cada PR deve manter:

```text
legacy tests green
brooks tests green
```

---

# 29. Onde parar para testar cedo

Não esperar PR-14.

## Checkpoint A — após PR-06

Rodar somente Trader:

```text
H1 close
→ LLM
→ TradeIntent
```

Coletar dezenas/centenas de decisões.

Validar:

```text
schema
latency
tool usage
consistency
lookahead
decision frequency
```

## Checkpoint B — após PR-08

Demo trading:

```text
Trader
→ GM
→ Hummingbot
```

Sem PM dinâmico ainda.

A posição nasce com SL/TP/time limit.

## Checkpoint C — após PR-11

Sistema completo:

```text
Trader
→ GM
→ Hummingbot
→ Position Watcher
→ PM
→ HOLD/REDUCE/CLOSE
```

Esse é o primeiro release que vale colocar em execução contínua no ambiente demo.

---

# 30. Critério de sucesso do MVP

Arquitetura:

```text
PASS
```

quando:

1. `execution_mode=loop` continua intacto;
2. H1 fecha e Trader acorda uma vez;
3. Trader só enxerga mercado;
4. Trader usa skills/tools;
5. Trader produz `TradeIntent`;
6. D1 Analyst atualiza `MarketContext`;
7. GM recebe intent e calcula sizing determinístico;
8. Hummingbot abre a posição;
9. tese original é persistida;
10. Trader continua emitindo intents durante a posição;
11. PM acorda por timer e eventos;
12. PM recebe original + latest intent + HTF context;
13. PM pode consultar tools limitadas;
14. PM produz `ManagementDecision`;
15. GM valida antes de write;
16. execution dispatcher executa;
17. tudo fica reconstruível pelo Event Store;
18. restart não perde a relação entre posição e tese.

---

# 31. Resumo final

Não estamos criando outro sistema ao lado do Condor.

Estamos criando um novo modo dentro dele:

```text
CONDOR
  │
  ├── legacy agents
  │      └── execution_mode=loop
  │
  └── Brooks Agents
         └── execution_mode=brooks_agents
                │
                ▼
            EVENT BUS
          ┌─────┼─────┐
          ▼     ▼     ▼
       Trader  HTF    PM
          │     │     │
          └── shared state
                │
                ▼
               GM
                │
                ▼
        HummingbotExecutionPort
                │
                ▼
          Hummingbot API
```

O ganho arquitetural central é:

```text
LLM = raciocínio
skills = conhecimento
tools = investigação
contracts = comunicação
GM = regra dura
Hummingbot = execução
event store = verdade/auditoria
```

E a fronteira do PM fica exatamente no meio-termo desejado:

```text
NÃO:
prompt com centenas de candles

SIM:
posição + tese + latest intent + HTF context
              +
tools limitadas sob demanda
              +
get_candles(limit <= 30)
```

Isso mantém o PM como gestor agentic sem duplicar o Trader e sem abandonar a infraestrutura que o Condor já oferece.
