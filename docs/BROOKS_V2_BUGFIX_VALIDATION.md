# Brooks V2 — correções dos defeitos comprovados

Data: 30/09/2026. Branch: `feat/brooks-agents-v1`. Base da implementação:
`a0cb1ffcf9a36deccf95f1e1fcf06334a40e64bd`.

Este documento registra implementação e validação posteriores à
[investigação](BROOKS_V2_INVESTIGATION_REPORT.md). A investigação e seus snapshots
fixos permanecem como evidência do comportamento anterior.

## Escopo e sequência

1. Corrigir ownership persistido após fechamento e recuperação no restart.
2. Fechar HEDGE órfã somente com identidade comprovada, através do GM/ExecutionPort.
3. Corrigir inconsistências de representação e recuperar falhas de output de forma limitada.
4. Preservar erros de transporte para o retry existente; restaurar fatos disponíveis no PM/replay.
5. Validar os caminhos críticos e a regressão Brooks/Condor; commitar e enviar ao fork.

As tarefas de contrato/runtime e fidelidade PM foram delegadas a Luna com esforço
máximo. A reconciliação de lifecycle e sua integração foram implementadas pelo
coordenador. Não foi criado serviço, agente ou fluxo de inferência adicional.

Não houve alterações em `agents/brooks_price_action/skills/`, modelo, escolha de
timeframes, janelas de 120 barras, compressão macro, regras de seleção Brooks,
timeouts, prioridade/serialização de backend ou cálculo de risco/quantity.
As hipóteses SKL-01 e IN-01 e a avaliação de capacidade preditiva ficam fora desta rodada.

## Correções e evidências

| Finding | Correção | Evidência reproduzível |
|---|---|---|
| LIF-01, LIF-03 | `BrooksGM.reconcile_lifecycle()` confirma duas leituras frescas completas, exige executor encerrado ou ausência corroborada, arquiva IDs e persiste `closed`. O watcher solicita essa reconciliação também no primeiro snapshot flat e em snapshots sem mudança. | `tests/test_brooks_binding_lifecycle.py`; próxima entrada aceita; evento `BINDING_CLOSED` persistido. |
| LIF-02 | MAIN encerrado com HEDGE própria entra em `main_closed`; GM fecha a quantidade real da HEDGE com `position_action=CLOSE`. Identidade, lado, modo, quantidade, executores e ausência de exposição extra precisam concordar em duas leituras. | Testes de orphan explícito e `executor:<id>`, exposição extra/foreign, shadow e timeout/restart. |
| OUT-01 | Schema exige `M15` em decision timeframe e sources de trigger/invalidation; a única normalização adicional é `15m → M15`. H1/H4/D1 continuam rejeitados. | `tests/test_brooks_entry_reference_contract.py`; revalidação de respostas reais abaixo. |
| OUT-03 | Preço precisa ser o mesmo Decimal finito, sem tolerância. `2638.00` e `2638` são equivalentes; índice, campo e timestamps ainda precisam coincidir exatamente. Janelas M15 consultadas durante o mesmo role são observadas pelo host. | Testes de decimal exato, índice/timestamp incorreto e janela menor consultada por tool. |
| OUT-02 | Uma única correção de JSON/schema/referência, no mesmo role e mesmo input congelado, antes da falha definitiva. Markup ou resultados inventados não são despachados como tools. Violações de privacidade continuam falhando imediatamente. | `tests/test_brooks_prompt_runtime.py`; auditoria `contract_repair`; validator de referências ligado ao loop real do Trader. |
| Schema/price location | Exceção restrita à string de localização em `market_context.m15_facts.position`, com formato de localização no range. Objetos de posição/conta e outros caminhos continuam proibidos. | `tests/test_brooks_contracts.py`; resposta real do round 5 agora válida. |
| RUN-01 | `PydanticAIClient.prompt()` propaga a exceção original de timeout/provider e sua causa para a classificação transitória existente. Streaming mantém seus eventos públicos. | `tests/test_pydantic_ai_prompt_error_propagation.py` e regressões Pydantic/ACP. |
| PM-01 | Loader entrega entry price/PnL do mesmo read que resolveu ownership, fills próprios e campos conhecidos de barreiras/custos/executor. Fills indisponíveis são explicitamente marcados e não inutilizam a gestão. | `tests/test_brooks_pm_fidelity.py`; E2E HOLD/REDUCE/hedge. |
| PM-02 | Análise fresca default usa as tools read-only existentes vinculadas ao símbolo; remove a chamada ao método inexistente `as_tools()`. | `tests/test_brooks_pm_runner.py`. |
| SIM-01 | Latest intent/context aceita strategy home ou store root, sem duplicar `brooks_state`. Simulador aceita a assinatura real de `search_orders` e trata `FILLED` como um status único. | Regressões PM; `tests/test_brooks_walkforward_fidelity.py`. |
| SIM-02 | Pending stop não é convertido em MARKET. GM rejeita explicitamente antes de leitura, persistência ou ordem; o intent e a razão de rejeição permanecem auditáveis. | `tests/test_brooks_gm.py`, incluindo passagem pelo GMConsumer. |

### Lifecycle e fronteira de write

- O GM permanece responsável pela persistência de ownership. O watcher continua
  observando mercado/venue: seu callback apenas solicita ao GM prova independente.
- MAIN ainda positiva, executor RUNNING/SHUTTING_DOWN, ordens pendentes, leituras
  discordantes, páginas incompletas ou ownership irresolvido não liberam binding.
- Paginação de posições/executores/ordens é verificada tanto no nível principal
  quanto em `pagination.next_cursor`. Linhas ilegíveis não comprovam flat.
- MAIN pode ter encerrado antes de a reconciliação inicial observar seu position ID.
  Nesse caso, um executor explicitamente terminal e duas leituras flat permitem
  encerrar `submitted`; simples ausência de executor não libera essa reserva.
- O fechamento órfão usa somente o ExecutionPort existente e a quantidade atual
  comprovada da HEDGE; não altera os guards normais de compilação de hedge.
- `lifecycle_cleanup.json` é persistido **antes** da write. Receipt não significa
  fill. Timeout/erro ambíguo mantém `reconciliation_required` nesse registro;
  polls e restart não reenviam a ordem. Flat corroborado posteriormente permite
  concluir o registro e encerrar o binding.
- `closed_*` conserva a linhagem histórica, enquanto os campos de IDs ativos são
  limpos. `executions.jsonl` registra as transições. `read_bindings()` inclui
  `main_closed` enquanto ainda há trabalho de reconciliação e exclui `closed`.
- Shadow permite reconciliação local comprovada, mas nunca envia a limpeza à venue.

O status numérico `3` significa SHUTTING_DOWN; `4` significa TERMINATED, conforme
o [enum oficial Hummingbot](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy_v2/models/base.py).
Os testes verificam que `3` não autoriza liberação/cleanup.

### Fidelidade do PM e simulador

O PM só recebe fatos ligados aos IDs resolvidos do trade. Campos financeiros e
barreiras de payload arbitrário são filtrados por allowlist. Funding e custos
ausentes não são estimados. A posição não é buscada novamente para enriquecer o
packet: os fatos são da mesma leitura que estabeleceu ownership.

`recent_fills` conserva fills próprios anteriores ao último evento;
`fills_since_last_event` é filtrado separadamente. Timestamp conhecido posterior
ao decision time é excluído. Falha do endpoint opcional mantém listas vazias e
`executor_state.fills_read_status=unavailable`, sem declarar que houve zero fills.

O simulador agora expõe o position ID e fee efetiva nos fills, além dos preços de
stop/target já calculados na abertura. Order executor de HEDGE já preenchida é
terminal; executor MAIN segue ativo até sua barreira/fechamento.

## Revalidação das respostas reais

Arquivo: [recorded_output_revalidation.json](brooks_v2_bugfix_validation/recorded_output_revalidation.json).
Releitura offline das 25 falhas baseline, sem alterar responses ou chamar o LLM.
O probe verifica schema e referências da janela inicial; **não** representa uma
nova rodada Trader/GM nem dispensa os gates de freshness/coverage.

| Categoria anterior | Passam agora | Continuam rejeitadas |
|---|---:|---:|
| Alias `15m/M15` | 10 | 1 |
| Schema | 1 | 3 |
| JSON/protocolo | 0 | 8 |
| Coordenadas OHLC | 0 | 2 |

O alias restante, `clock-1h-1789873199999-6`, também contém `ENTER_SHORT` com
`trigger_status=pending`, `kind=limit`, `direction=above`. A normalização do
timeframe não apaga esse erro independente: o guard de pending continua exigindo
stop na direção da entrada. O round 139 passa agora, inclusive sua representação
decimal `2638.00`. JSON e coordenadas incorretas permanecem inválidos quando a
resposta antiga é reavaliada isoladamente; a recuperação limitada depende de uma
nova resposta válida do mesmo role, exercitada pelos testes do runner.

## Validação automatizada

Os testes de lifecycle incluem restart flat, executor terminal antes da primeira
reconciliação, ambiguidades de identidade/exposição, duas leituras discordantes,
timeout de cleanup sem repetição e shadow sem writes.

O teste integrado do adaptador walk-forward exercita cada fechamento:
`STOP_LOSS`, `TAKE_PROFIT`, `TIME_LIMIT`, `PM_CLOSE`.
Em todos: MAIN/reconciliation → packet PM com fills/barreiras → fechamento
confirmado → `POSITION_CLOSED`/`BINDING_CLOSED` → binding terminal → nova entrada.
São fixtures determinísticas com componentes de produção; não são decisões novas
do DeepSeek nem demonstração de execução na Binance.

Execução final: **668 passed, 16 warnings, 49,54 s**. Os warnings são de nomes de
campos `schema`/depreciações já presentes, não falhas de regressão.

```sh
.venv/bin/python -m pytest -q tests/test_brooks_*.py tests/test_pydantic_ai_*.py \
  tests/test_condor_tool_surface.py tests/test_condor_is_an_agent.py \
  tests/test_condor_migration.py tests/test_fetcher_executors.py \
  tests/test_fetcher_positions.py tests/test_fetcher_historical_candles.py \
  tests/test_acp_permission_gate.py
```

Esse comando inclui os seis arquivos de validação solicitados, toda a suíte
Brooks, os cenários novos e as regressões Condor relevantes. Antes dele,
`git diff --check` passou. Não foram realizadas ordens externas nos testes.

## Limites e continuidade

1. **Pending stop nativo permanece indisponível.** Esta rodada impede fills
   falsos; não adiciona scheduler de entrada ou implementa um recurso de venue
   não comprovado. Um ENTER pending válido pode ser rejeitado pelo GM com razão
   explícita. Isso é distinto de NO_TRADE e deve constar nos denominadores do
   próximo forward. Não apresentar replay de MARKET como teste de entradas stop.
2. MAIN ausente com executor ainda ativo não é fechamento confirmado. Fechamento
   externo/redução total que deixe executor ativo exige settlement/reconciliação
   da venue; o binding não é liberado pela mera ausência da posição.
3. Cleanup ambígua não é reenviada automaticamente se HEDGE continuar positiva.
   O registro preserva a necessidade de recuperação, sem duplicar writes.
4. Não foi executada nova demo/live nem lote de LLM nesta validação. O replay antigo
   foi pausado com checkpoint em 236 ciclos e seu source HEAD anterior preservado
   no manifest. Seus resultados não são evidência das correções.
5. DATA-01 não recebeu alteração: o consumer atual já possui lock/idempotência por
   identidade e contexto persistido. Capturas repetidas antigas não são removidas
   nem contadas como contextos independentes. OUT-04 permanece uma questão de
   narrativa/raciocínio, sem mudança de Skill nesta rodada.
6. Os testes não comprovam comportamento de settlement/404 da API real sob todos
   os atrasos de Binance. Os adapters mantêm desconhecido como bloqueio.

O próximo trabalho deve ser validação shadow/out-forward com esses denominadores
e limitações explícitos. Não há conclusão de edge ou ajuste de hipótese preditiva
com base no lote antigo contaminado.
