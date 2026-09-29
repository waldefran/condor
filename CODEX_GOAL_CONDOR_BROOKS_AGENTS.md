/goal Implementar o modo `brooks_agents` dentro do Condor atual, trazendo a separação Trader / HTF Analyst / deterministic GM / Position PM do brooks-harness e usando as skills do Skill-Brooks, SEM criar outro harness e SEM alterar a Hummingbot API no V1.

LEIA PRIMEIRO, ANTES DE EDITAR QUALQUER CÓDIGO:
1. `CONDOR_BROOKS_AGENTS_IMPLEMENTATION_PLAN.md` na raiz do repositório.
2. O código atual do Condor relacionado a runtime, tools, prompts, risk, Hummingbot MCP e executors.
3. `/home/valdemaster/walde_projects/brooks-harness/`, especialmente contracts, ClosedBarGate, Trader/PM validators, market-analysis boundary e hedge semantics.
4. `/home/valdemaster/walde_projects/binatrade/Skill-Brooks/`, especialmente:
   - `brooks-market-context`
   - `brooks-trade-entry`
   - `brooks-position-management`

REGRAS DE GIT — OBRIGATÓRIAS:
- Antes de editar: `git status`, `git remote -v`, `git branch --show-current`, `git log -5 --oneline`.
- Identifique explicitamente o upstream oficial e o meu fork/remoto pessoal gravável.
- NUNCA faça push para `hummingbot/condor`.
- Trabalhe em branch `feat/brooks-agents-v1` ou nome equivalente compatível com a convenção atual.
- Faça commits pequenos por feature concluída.
- Depois de cada feature: testes alvo + testes relevantes existentes + revisão do diff + commit.
- Faça push incremental SOMENTE para meu remote pessoal gravável.
- Se não houver remote pessoal configurado, continue commitando localmente e reporte isso; NÃO improvise push para upstream.
- Não faça squash durante desenvolvimento.
- Não reescreva histórico existente.

MODO DE TRABALHO:
Você é o Integrator. Delegue agressivamente a subagents para acelerar o trabalho, mas mantenha controle arquitetural e integração final.

Crie subagents independentes sempre que houver trabalho paralelo real, por exemplo:
A. runtime/execution_mode/event lifecycle;
B. contracts/fixtures/ClosedBarGate;
C. Trader/HTF/tool isolation;
D. deterministic GM/sizing/HummingbotExecutionPort;
E. PositionWatcher/PM;
F. hedge state/compiler/execution;
G. adversarial tests/replay.

Evite conflito:
- dê ownership de arquivos claro;
- prefira worktrees/branches isoladas quando suportado;
- não deixe dois subagents editarem o mesmo núcleo simultaneamente;
- peça a cada subagent findings + mudanças + testes;
- revise tudo antes de integrar;
- o Integrator é responsável pelo branch principal, gates e push.

ARQUITETURA QUE DEVE EXISTIR AO FINAL:

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

NÃO transforme isso em um pipeline PM→Trader→GM dentro de um único tick.
Trader, HTF e PM possuem triggers/cadências independentes e comunicam por estado/eventos.

CONDOR DEVE CONTINUAR SENDO O HARNESS:
- novo `execution_mode: brooks_agents`;
- `execution_mode: loop` e os agentes atuais não podem mudar de comportamento;
- reutilize lifecycle, ACP/Pydantic-AI, model selection, skills, MCP, allowlists/mutes, journal e Hummingbot integration existentes;
- não introduza Hermes, Pi, LangGraph, Redis, Kafka ou outro runtime.

TRADER:
- trigger inicial: `H1_BAR_CLOSED`;
- pode usar skills e safe market tools;
- NÃO recebe account, balance, equity, positions, PnL, executors, orders ou margin;
- NÃO possui nenhuma write tool;
- output: `brooks.trade-intent.v2`;
- decisões: `ENTER_LONG | ENTER_SHORT | NO_TRADE`;
- continua analisando mesmo com posição aberta;
- persista original/latest/history.

HTF ANALYST:
- trigger inicial: `D1_BAR_CLOSED`;
- skill principal: `brooks-market-context`;
- output: `MarketContextV1`;
- sem dados de conta;
- contexto fica disponível para Trader e PM.

PM:
- triggers: timer + POSITION_OPENED/CHANGED + FILL + HEDGE_CHANGED + nova TraderIntent + novo MarketContext quando há posição ativa;
- é um LLM agentic: skills + tools + structured output;
- input inicial NÃO contém dump grande de candles;
- recebe posição/executor/orders/fills, tese original, última TraderIntent, HTF context, management history, management policy, hedge state e margin health;
- pode usar tools limitadas:
  - `get_market_context()`
  - `get_recent_structure()`
  - `get_volatility()`
  - `get_latest_trader_intent()`
  - `get_original_trade_intent()`
  - `get_position_state()`
  - `get_executor_state()`
  - `get_open_orders()`
  - `get_recent_fills()`
  - `get_candles(symbol,timeframe,limit<=30)`
- todos os candles passam pelo ClosedBarGate;
- PM NÃO recebe write tools;
- output: `brooks.management-decision.v2`.

GM:
- determinístico, não LLM;
- único ponto de autorização/compilação de mutações;
- sizing;
- exposure;
- margin;
- position count;
- leverage policy;
- quantization;
- ownership MAIN/HEDGE;
- locks;
- state revalidation imediatamente antes do write.

MAIN:
- abrir preferencialmente por `create_position_executor`;
- incluir SL/TP/time_limit quando definidos;
- persistir `correlation_id`, `controller_id`, `executor_id` e original TradeIntent.

HEDGE — INCLUIR NO V1:
NÃO alterar Hummingbot API.
Use primitives já existentes.
- garantir/confirmar `PositionMode.HEDGE`;
- hedge leg via `create_order_executor`;
- abrir/aumentar hedge: opposing side + `position_action=OPEN`;
- reduzir/remover hedge: opposing close + `position_action=CLOSE`;
- suportar:
  - HEDGE
  - INCREASE_HEDGE
  - REDUCE_HEDGE
  - REMOVE_HEDGE
- `ratio_basis = absolute_mark_notional`;
- target ratio decimal canônico em `[0,1]`;
- MAIN/HEDGE ownership explícito;
- nunca inferir role por side, size, order ou PnL;
- unresolved structure => fail closed / RECONCILE_STATE / MANAGEMENT_BLOCKED;
- reutilize as invariantes hedge-aware já consolidadas no Skill-Brooks/brooks-harness.

HEDGE COMPILER:
- `target_hedge_notional = abs(main_mark_notional) * target_ratio`;
- executar apenas delta entre target e current;
- HEDGE: single_main, current=0, target>0;
- INCREASE: structure ok, target>current;
- REDUCE: structure ok, 0<=target<current;
- REMOVE: structure ok, target==0;
- revalidar tudo imediatamente antes do write.

AÇÕES PM V1:
- HOLD
- REDUCE
- CLOSE
- HEDGE
- INCREASE_HEDGE
- REDUCE_HEDGE
- REMOVE_HEDGE
- REQUEST_MARKET_ANALYSIS
- RECONCILE_STATE
- MANAGEMENT_BLOCKED

Não implemente PROTECT/MOVE_PROTECTION por hack. A MAIN já nasce com proteção via PositionExecutor. Só implemente proteção dinâmica posteriormente se houver primitive Hummingbot segura e testada.

CLOSED BAR:
porte a semântica do `brooks-harness/src/market/closed-bar-gate.ts`.
Nenhuma barra forming/futura pode chegar ao raciocínio Brooks.
Valide ordering, duplicates, OHLC, gaps e close_time<=decision_time.

EVENT BUS:
- MVP com `asyncio.Queue`;
- não adicionar infraestrutura externa;
- persistir eventos em JSONL;
- correlation_id/causation_id;
- restart deve reconstruir relação MAIN/HEDGE/tese.

STATE:
- payload durável em `<strategy_home>/brooks_state/`;
- `BoundState` somente para cursors pequenos;
- escrita atômica de latest/binding/hedge state;
- JSONL para history/events/executions.

CONCORRÊNCIA:
- lock por account+connector+symbol para writes;
- LLMs podem rodar em paralelo;
- mutações não;
- re-read venue state depois de adquirir lock;
- ambiguous execution => não presumir sucesso, exigir reconciliation.

EXECUTION PORT:
crie uma abstração mínima dentro do Condor, mas implemente somente `HummingbotExecutionPort` no V1.
Não implemente Binance/Hyperliquid direto agora.

FEATURE ORDER — siga exatamente salvo impedimento técnico real:
1. isolated `brooks_agents` execution mode;
2. contracts;
3. event bus + durable store;
4. ClosedBarGate + safe market tools;
5. role agent runner;
6. Trader;
7. HTF Analyst;
8. deterministic GM + MAIN entry;
9. PositionWatcher;
10. Position PM;
11. PM HOLD/REDUCE/CLOSE execution;
12. Hedge Mode + HedgeState + compiler;
13. hedge lifecycle execution;
14. REQUEST_MARKET_ANALYSIS;
15. replay/evidence.

COMMIT após cada feature verde. Use mensagens semânticas similares às descritas no plano.

TESTES:
- preserve toda suíte existente;
- unit/contract/adversarial para o novo modo;
- fake Hummingbot para unit;
- demo/testnet para integração real;
- não dependa de live funds para CI.

HEDGE ADVERSARIAL obrigatório:
- duplicate MAIN/HEDGE;
- orphan hedge;
- unknown role;
- same-side legs;
- invalid ratio;
- invalid action/target relation;
- stale snapshot;
- partial fill;
- venue state changed before write;
- ambiguous execution;
- restart com MAIN+HEDGE abertos.

IMPACTO ZERO:
Não quebre nem reescreva os agentes atuais.
Não altere prompts/toolset globalmente de forma que mude `execution_mode=loop`.
Prefira novos módulos em `condor/brooks/` e wiring mínimo nos pontos existentes.

ESCOPO:
Não pare para “melhorar arquitetura” fora do plano.
Não introduza features cosméticas.
Não construa dashboard.
Não faça refactor geral.
Não troque stack.
Não altere Hummingbot API sem prova concreta de bloqueio e, se encontrar um bloqueio real, documente-o antes de qualquer mudança.

EXECUÇÃO:
Comece agora pela auditoria curta do código e divisão dos subagents.
Depois implemente feature por feature.
Não me peça confirmação entre features normais.
Faça commits/push incrementais no meu fork pessoal.
Ao final de cada milestone, produza um resumo curto:
- commits;
- testes;
- comportamento demonstrado;
- pendências reais.

MILESTONES:
A. Trader shadow;
B. Trader→GM→MAIN em demo;
C. PM HOLD/REDUCE/CLOSE;
D. hedge completo HEDGE/INCREASE/REDUCE/REMOVE;
E. replay/restart evidence.

DEFINITION OF DONE:
O trabalho termina apenas quando o V1 completo descrito em `CONDOR_BROOKS_AGENTS_IMPLEMENTATION_PLAN.md` estiver implementado, testado, commitado e pushado no meu fork pessoal, ou quando existir um bloqueio externo concreto que impeça uma feature. Nesse caso, deixe todo o restante verde e documente o bloqueio com evidência de código/API.
