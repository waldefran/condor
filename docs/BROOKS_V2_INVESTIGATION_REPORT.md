# Brooks V 2 — investigação do lifecycle, outputs e decisões

**Escopo:** investigação, reprodução offline e evidência; nenhuma correção de código, prompt, contrato, estratégia ou arquitetura. Branch inspecionada: `feat/brooks-agents-v1`, HEAD inicial `7760041af8a6d2ea6ed737ddc7a8d86cbfeb305c`.

## 1. Executive summary

A causa do bloqueio de lifecycle está comprovada: **o encerramento confirmado do MAIN não tem uma transição correspondente no binding persistido**. O watcher emitiu `POSITION_CLOSED`; não faltou detectar o STOP neste replay. `read_bindings()` continua selecionando o binding `reconciled`, o account reader encontra um MAIN inexistente e produz `main_binding_without_venue_position`. O GM rejeita novas entradas; o PM não recebe um contexto válido para aquela posição. A reconciliação de abertura não reconcilia encerramentos.

O mesmo código de binding, reader, watcher e GM é usado em demo/live. A exposição em produção está comprovada pelo wiring e pelas reproduções com os adaptadores reais; **os artefatos não demonstram que esse incidente já ocorreu em uma conta real**. MAIN encerrado com HEDGE remanescente é uma variante mais grave: a exposição permanece e o loader PM recusa o contexto.

Há problemas independentes no output e no harness. As 11 falhas descritas antes como “fora de M15” usam **`source.timeframe: "15m"`**, não H1/H4/D1. Nos dez primeiros casos, as duas referências eram candles M15 corretos com OHLC, índice e timestamps exatos; falharam na representação literal. Também há texto de tool calling incompatível com o protocolo JSON, serialização inválida, violações do schema e cópia incorreta de referências. O transporte é PydanticAI/OpenCode API, mas a requisição não impõe structured output nativo: o schema vai como texto.

A Skill transmite conceitos Brooks relevantes e há decisões defensáveis, ligadas a barras concretas. Sua execução é desigual: a descrição frequentemente é específica, mas a ação conservadora e algumas condições futuras são repetitivas; referências são pouco usadas e não há prova de que cada contagem de padrão foi aplicada corretamente. Não está demonstrado que o modelo seja incapaz, que aumentar o prompt ajude ou que outro modelo seja melhor.

**Não é possível avaliar edge com este lote.** Após o único trade encerrado, o lifecycle contamina a execução. Há ainda diferença entre stop `pending` descrito pelo Trader e a execução MARKET efetiva, e perda de contextos recentes no input PM exclusiva do harness. Uma operação fechada não permite inferir lucro esperado, capacidade preditiva ou qualidade estatística da estratégia.

## 2. Replay facts

### Bases fixadas e proveniência

A evidência principal é a pasta [do replay](brooks_walkforward_2026-09-20_2026-09-29/) no commit `7760041a`. O snapshot posterior foi congelado uma única vez em **30/09/2026 12:12:09 UTC**, sem consultar continuamente o progresso. Os números abaixo são desses snapshots, não um placar em tempo real.

| Medida | Baseline commit `7760041a` | Snapshot posterior |
|---|---:|---:|
| Ciclos H1 registrados | 145 | 189 |
| Ciclos concluídos com intent aceito pelo host | 120 | 159 |
| Ciclos falhos, sem intent aceito | 25 | 30 |
| NO_TRADE válido | 97 | 134 |
| ENTER_LONG válido | 20 | 22 |
| ENTER_SHORT válido | 3 | 3 |
| GM_ENTRY_APPROVED | 1 | 1 |
| GM_ENTRY_REJECTED | 22 | 24 |
| Sessões PM concluídas / falhas | 40 / 7 | 40 / 7 |
| Context Analyst concluídas / falhas / em andamento | 43 / 2 / 1 | 57 / 2 / 0 |
| Requisições HTTP capturadas, todos os roles | 331 | 400 |

Falha de ciclo não é `NO_TRADE`. `role_runs.status=completed` também não basta para afirmar aceitação: no harness, `TraderConsumer.handle()` pode retornar `None` após persistir um ciclo falho. A contagem usa `cycles.status`, intent e eventos, não apenas o status do wrapper de captura.

Os manifests registram o source HEAD `a83fb54cf2a770be73119190a6b31d27f73f3f21`, quando o processo foi iniciado. Entre esse commit e `7760041a`, **não houve diferença em `condor/`, `agents/` ou `tests/`**; nos scripts só mudou o renderer `brooks_walkforward_report.py`. Não é divergência da versão de estratégia/lifecycle. Por isso relatórios são evidência secundária, e a investigação recalcula fatos de ciclos, eventos, captures e ledger. Hashes e fontes: [manifest.json](brooks_v2_investigation_evidence/manifest.json).

### Fluxo, custos e limites

- ETH-USDT, lote planejado de 20/09 00:00 UTC a 30/09 00:00 UTC exclusivo, 240 despertares H1; os snapshots ainda não eram o lote completo.
- LLM real: `custom@opencode-go:deepseek-v4.1-flash`; modelo no HTTP body `deepseek-v4.1-flash`; endpoint `https://opencode.ai/zen/go/v1/chat/completions`.
- Venue de execução exclusivamente simulada. O nome `binance_perpetual_demo` no binding é o connector configurado no adapter offline, não evidência de uma ordem enviada à Binance.
- GM, reader, watcher e consumers de produção; histórico fechado congelado por role. A latência real avança o mercado simulado, não o packet já enviado ao modelo.
- Equidade inicial 10.000 USDT; taxa taker 0,04% por lado; slippage adverso de 1 bp; funding não modelado. Barreira em M 1 posterior completo; stop primeiro se ambas as barreiras forem tocadas. Primeira fração de minuto após entrada não resolve barreiras/excursões.
- Único trade: SHORT `0.478`, fill de entrada `2583.861588`, saída STOP `2594.829456999…`, líquido simulado **−6,232807 USDT / −1,247784 R**. Este é um fato ex-post, não argumento para julgar a decisão ex-ante.

Auditoria fixa: 34.800 barras H1/M15 baseline e 45.360 posteriores; nenhum hash de packet divergente, candle/contexto futuro ou ciclo duplicado. Três retries conservaram a mesma primeira mensagem user; intents/event IDs únicos: 120/159. Máximo de intervalos de `client.prompt()` capturados simultâneos: 1. Isso verifica o comportamento observado do cliente, não mede concorrência interna do serviço de inferência. [Prova de integridade](brooks_v2_investigation_evidence/snapshot_integrity.json).

## 3. Binding lifecycle investigation

### Responsabilidades efetivas

| Componente | O que faz hoje | O que não faz no encerramento |
|---|---|---|
| `BrooksGM.execute_entry()` | Cria o binding; persiste `submitting`, depois `submitted`; grava executor e intenção original | Não agenda um lifecycle terminal |
| `BrooksGM._try_reconcile_binding()` / `reconcile_main()` | Associa a execução de abertura ao MAIN e marca `reconciled` | `reconcile_main()` retorna imediatamente se já há binding reconciled com ID; não verifica se o MAIN encerrou |
| GM de management/hedge | Persiste decisões, receipts e atualiza IDs/estado de hedge em caminhos específicos | CLOSE MAIN grava receipt `submitted`, sem encerrar o binding |
| `ExecutionPort` / executor | Executa abertura, redução/close; stop/TP/time limit podem encerrar a posição | Não é o proprietário da persistência Brooks de binding |
| `build_watcher_provider()` / `PositionWatcher` | Junta venue com bindings e emite mudanças/fechamento | É observador; não altera `binding.json` |
| PM | Seleciona gestão quando `load_context()` fornece uma posição MAIN coerente | Recebe `None` quando MAIN já não existe; não produz decisão que encerre o binding |
| GM consumer/supervisor | Roteia intents Trader/PM ao GM | O GM consumer não assina `POSITION_CLOSED`; o evento não é traduzido em transição determinística de binding |
| `read_bindings()` / account reader | Seleciona bindings não terminais e resolve ownership contra venue fresca | Binding `reconciled` ausente na venue vira estrutura irresolvida, não flat reconciliado |

Não existe lifecycle terminal explícito com transição, confirmação de flat e consumidor responsável por ela. `read_bindings()` aceita `submitting`, `submitted`, `reconciled`, `reconciliation_required`; um status desconhecido seria excluído, mas isso **não constitui uma implementação de binding terminal**. `TradeBindingV1` também não define um enum de lifecycle terminal. `planned_quantity` no binding é quantidade planejada, não o saldo atual da posição.

Fontes centrais: [GM](../condor/brooks/gm.py), [adapters](../condor/brooks/adapters.py), [watcher](../condor/brooks/position_watcher.py), [supervisor](../condor/brooks/supervisor.py), [execution](../condor/brooks/execution.py).

### Timeline concreta — `ETH-USDT-1h-1789876799999`

Tempos abaixo são UTC do relógio virtual. `events.created_at_ms`, fills e executor exits são usados para ordenar mercado; campos de captura `started_at_ms`/`finished_at_ms` e alguns `cycle`/`gm_result.created_at_ms` são tempo de parede. Não misturar esses relógios.

| Instante virtual / epoch ms | Evento / ID | Estado comprovado |
|---|---|---|
| 20/09 04:00:02; cutoff `1789876799999` | H1 fechado `8aa46089-7942-4cfa-a1e1-63cf7cf4c265` | Packet congelado do Trader |
| 04:04:36.283 | Intent `7ec6bace-73c0-5037-87d0-35f7c536bfc4` | ENTER_SHORT; trigger stop `pending`, referência `2569.8`, invalidation `2594.57` |
| 04:04:36.410 / `1789877076410` | GM aprovado `b0d6812b-27a4-44f5-8f89-4b7f7cf3635f`; fill `wf-fill-1` | MAIN `wf-main-1`, executor `wf-exec-1`, SHORT `0.478`; binding reconciled; MARKET fill `2583.861588` |
| 04:04:59.999 | POSITION_OPENED `158285f5-739b-4bd9-9ff7-e744c8aeb306` | Watcher observa MAIN SHORT `0.478` |
| 04:07:43.957 a 14:45:28.548 | 40 GM_MANAGEMENT_APPROVED | Todas HOLD; nenhum PM CLOSE/REDUCE no trade histórico |
| cutoff 14:59:59.999 / `1789916399999`; publicação H1 15:01:00.173 | H1 `7f2423eb-321c-4825-b8ee-6a58a1d1d6d0` | Novo cutoff precede o STOP; GM ainda não avaliou a resposta |
| 15:01:59.999 / `1789916519999` | Fill `wf-fill-2`, saída `wf-exec-2`, reason STOP_LOSS | MAIN restante `0.000`; trade `closed`; executor principal `wf-exec-1` CLOSED; fill `2594.829456999…` |
| Mesmo instante | POSITION_CLOSED `a94f73ed-1b3d-4e29-812a-dea0284bf889`; ORDER_CHANGED `73f9e2b6-e99b-40e8-aabf-9703420c9ea3` | Watcher observa MAIN `0.478 → 0`, posição ausente |
| Estado exportado pós-STOP, idêntico nos dois snapshots | `binding.json` do CID original | `reconciled`, MAIN `wf-main-1`, executor `wf-exec-1`, planned `0.478`; sem transição terminal |
| 15:04:20.861 | Novo intent `14d18232-2952-5903-94bd-62f57815edc4` | ENTER_LONG do cutoff anterior; produzido após o STOP |
| 15:04:21.072 / `1789916661072` | GM_ENTRY_REJECTED `4061394a-57a6-4527-8f15-3f7637b345d8` | `position ownership or structure is unresolved` |

**Primeiro ponto de divergência:** `_close_position()` altera a verdade da venue para flat e encerra o trade/executor no STOP; não há atualização correspondente no binding. O arquivo não é uma amostra por milissegundo, mas seu estado posterior, a ausência de qualquer writer de encerramento e a reprodução com os leitores reais comprovam a divergência lógica. Ela antecede a rejeição do GM; o GM não criou o problema.

A leitura reproduzida pós-STOP retorna `open_positions=0`, `main_quantity=0`, `gross_exposure=0`, mas `main_position_id=wf-main-1` e `structure_status=main_binding_without_venue_position`. [Trace original](brooks_v2_investigation_evidence/lifecycle_trace.json) e [reprodução](brooks_v2_investigation_evidence/lifecycle_probe.json).

## 4. Exact root cause / unresolved hypotheses

### Causa comprovada

A lacuna é uma **responsabilidade ausente de reconciliação de encerramento**, composta por três fatos:

1. Persistência: nenhum caminho comum de fechamento MAIN promove o binding a um estado terminal confirmado.
2. Evento: POSITION_CLOSED é publicado, mas não há consumer determinístico de lifecycle que atualize esse ownership. PM não substitui esse consumer: seu loader exige MAIN aberto.
3. Leitura/recovery: binding reconciled permanece elegível; `reconcile_main()` trata a reconciliação de abertura como já concluída, enquanto o account reader corretamente acusa posição bound ausente.

Não é falta de candle, voto D1/H4, JSON do Trader, latência do PM ou ausência de POSITION_CLOSED neste caso. Essas questões podem coexistir, mas o binding stale e a rejeição são reproduzíveis sem LLM, sem market clock e sem Binance.

### O que ainda não está comprovado

- Como uma venue real apresenta seus últimos snapshots durante cada tipo de encerramento: desaparecimento, linha de quantidade zero, executor terminal retido/removido, fills parciais e ordens ainda pendentes. A exposição do código é demonstrável; a sequência real precisa de um experimento controlado separado.
- Quais guardas posteriores do GM rejeitariam cada entrada se o lifecycle estivesse íntegro. A primeira guarda de estrutura interrompeu a avaliação; não há prova de aprovação contrafactual de todas as propostas.
- Qual deve ser a política terminal com HEDGE ou ordens/executores remanescentes. Não se pode considerar flat apenas porque MAIN desapareceu.
- Se falhas de geração decorrem principalmente do alias de modelo, do protocolo textual, da densidade do input ou de sua interação. O lote não contém comparação controlada.

## 5. Production vs walk-forward exposure

| Caminho de encerramento | Evidência disponível | Exposição identificada |
|---|---|---|
| STOP_LOSS integral | Observado no trade e reproduzido com reader/watcher reais | MAIN flat, binding reconciled selecionável; GM bloqueia |
| TAKE_PROFIT | Inspeção do branch comum de `resolve_executor_bar()` e port com barreiras | Mesmo fechamento no sim; runtime não tem finalização de binding para TP |
| TIME_LIMIT | Inspeção do branch comum e port | Mesmo problema após um encerramento integral confirmado |
| PM CLOSE | Inspeção GM/port e reprodução de CLOSE com ACK seguido de flat | Receipt submitted persistido; bytes do binding permanecem idênticos após CLOSE na fixture |
| PM REDUCE parcial | Inspeção GM/port | Enquanto resta MAIN, manter binding é coerente; reader usa quantidade fresca |
| REDUCE total acidental | GM rejeita fraction ≥1 e quantidade quantizada ≥ MAIN | Não ocorreu; total intencional precisa CLOSE. Um resultado externo inesperadamente flat volta ao mesmo problema de lifecycle |
| Fechamento externo | Snapshot flat equivalente reproduzido offline | Watcher é read-only; não há writer terminal específico |
| Restart após flat | Watcher novo sem `initial_snapshots`, reproduzido | Primeiro poll não emite POSITION_CLOSED; binding continua stale e PM fica idle |
| MAIN flat com HEDGE aberto | Reproduzido com explicit position IDs | Exposição HEDGE `0.120`, gross `310.080` na fixture; MAIN ausente e PM context `None`. Tentativa de cleanup REMOVE_HEDGE reproduzida: rejeição orphan_hedge, zero writes |

O probe adicional também confirmou o problema com identidade `executor:wf-exec-1`: executor CLOSED/TERMINATED consultável resulta em `main_binding_without_venue_position`; removido resulta em `main_unresolved`. Em ambos, o GM recusa entrada antes de chamar open_main. [Extended probe](brooks_v2_investigation_evidence/lifecycle_extended_probe.json).

O wiring de produção em `adapters.wire_supervisor()` usa os mesmos adapters e GM contra o cliente Hummingbot configurado. `shadow_mode=True` é default; exposição a writes depende da configuração do deployment. Não foi iniciado teste com dinheiro, demo ou ordens externas nesta investigação.

O harness anterior tem uma diferença independente: passa `store.root` (`…/brooks_state`) ao loader PM, cujos helpers de contexto ambient acrescentam outro `/brooks_state`. Todas as 47 capturas PM têm `latest_trader_intent=null` e `latest_market_context=null`. Uma fixture com os mesmos dados retorna ambos os campos quando a raiz é `strategy_home`; retorna nenhum quando é `strategy_home/brooks_state`. O wiring de produção passa `strategy_home` e não reproduz essa omissão de path. [Prova](brooks_v2_investigation_evidence/pm_root_reproduction.json).

## 6. Post-close blocked entries analysis

A definição temporal importa:

| Critério | Baseline | Posterior |
|---|---:|---:|
| ENTER válido com **cutoff da decisão após** o STOP | 21 | 23 |
| ENTER cujo **evento de avaliação/rejeição GM ocorre após** o STOP | 22 | 24 |

A diferença é `ETH-USDT-1h-1789916399999`: seu cutoff precede o STOP em 120 segundos, mas sua publicação e avaliação GM são posteriores. Todas as decisões válidas listadas chegaram ao GM; todas as rejeições efetivas têm exatamente `position ownership or structure is unresolved`. Não há reason diferente misturado nesses eventos.

Isto comprova a **causa da rejeição observada**, não a admissibilidade hipotética de cada entrada. `compile_main()` checa estrutura antes de idade, drift, stop ainda protetivo, mínimos/notional, margem e demais caps. Essas guardas não foram alcançadas nas entradas rejeitadas. As falhas de protocolo/host descritas na seção seguinte são independentes e não chegaram ao GM como intents válidos.

Lista individual de propostas, IDs, cutoff, hora da rejeição e reason: [blocked_entries.json](brooks_v2_investigation_evidence/blocked_entries.json). A tabela individual é acrescentada abaixo.

Cada linha abaixo representa uma rejeição efetiva. `B` = presente no baseline; `P` = apenas no corte posterior. Todos têm host acceptance, venue flat, binding `reconciled` e o reason acima. O epoch no CID é o cutoff; a última coluna é o instante do evento GM, em UTC.

| Corte | Round / CID | Decisão | GM reject UTC | Event ID |
|---|---|---|---|---|
| B | `clock-1h-1789916399999-21` / `ETH-USDT-1h-1789916399999` | ENTER_LONG | 2026-09-20T 15:04:21.072+00:00 | `4061394a-57a6-4527-8f15-3f7637b345d8` |
| B | `clock-1h-1789919999999-22` / `ETH-USDT-1h-1789919999999` | ENTER_LONG | 2026-09-20T 16:03:53.605+00:00 | `aafc5419-6f2b-4bd6-af29-56dd46044314` |
| B | `clock-1h-1789934399999-27` / `ETH-USDT-1h-1789934399999` | ENTER_LONG | 2026-09-20T 20:09:44.951+00:00 | `81e01247-fe2f-4d7f-a10c-7736bdca8851` |
| B | `clock-1h-1789955999999-36` / `ETH-USDT-1h-1789955999999` | ENTER_SHORT | 2026-09-21T 02:09:06.413+00:00 | `da4725b7-247d-4dec-b875-36e5a0bd9041` |
| B | `clock-1h-1789959599999-37` / `ETH-USDT-1h-1789959599999` | ENTER_LONG | 2026-09-21T 03:04:47.964+00:00 | `2e2aa0a2-6933-45c6-a69c-b4a44960918d` |
| B | `clock-1h-1789984799999-46` / `ETH-USDT-1h-1789984799999` | ENTER_LONG | 2026-09-21T 10:02:29.018+00:00 | `be9737c1-4507-4bd4-98bb-a50992cccb85` |
| B | `clock-1h-1789988399999-47` / `ETH-USDT-1h-1789988399999` | ENTER_LONG | 2026-09-21T 11:03:35.885+00:00 | `e3637e6a-3e02-477e-bd21-fce3209a86cd` |
| B | `clock-1h-1789991999999-48` / `ETH-USDT-1h-1789991999999` | ENTER_LONG | 2026-09-21T 12:02:42.392+00:00 | `591f11bd-9b6d-4c12-97ff-fbec8c6826d1` |
| B | `clock-1h-1789995599999-50` / `ETH-USDT-1h-1789995599999` | ENTER_LONG | 2026-09-21T 13:02:18.582+00:00 | `b1fd7126-41d9-49fb-974f-adc4c6851e5a` |
| B | `clock-1h-1789999199999-51` / `ETH-USDT-1h-1789999199999` | ENTER_LONG | 2026-09-21T 14:02:01.615+00:00 | `d52cc818-3662-435b-a071-ba5b3c299cdc` |
| B | `clock-1h-1790009999999-55` / `ETH-USDT-1h-1790009999999` | ENTER_LONG | 2026-09-21T 17:04:30.300+00:00 | `142bbbbf-39c9-47b2-b013-0cec0ccf6379` |
| B | `clock-1h-1790035199999-63` / `ETH-USDT-1h-1790035199999` | ENTER_LONG | 2026-09-22T 00:02:23.755+00:00 | `bad374b0-0408-4d95-810c-6e45b62da25f` |
| B | `clock-1h-1790081999999-81` / `ETH-USDT-1h-1790081999999` | ENTER_LONG | 2026-09-22T 13:08:04.750+00:00 | `766818e8-97c6-4dd0-806e-9047a9e941ac` |
| B | `clock-1h-1790085599999-82` / `ETH-USDT-1h-1790085599999` | ENTER_LONG | 2026-09-22T 14:03:21.035+00:00 | `d78675cd-6df0-4a90-8ff3-491e52374aeb` |
| B | `clock-1h-1790089199999-83` / `ETH-USDT-1h-1790089199999` | ENTER_LONG | 2026-09-22T 15:03:02.474+00:00 | `70d3ea17-31b6-4cf1-a07e-7405a272257a` |
| B | `clock-1h-1790139599999-102` / `ETH-USDT-1h-1790139599999` | ENTER_LONG | 2026-09-23T 05:02:24.465+00:00 | `c424ad48-16a9-4b92-9629-80d9a4678694` |
| B | `clock-1h-1790164799999-110` / `ETH-USDT-1h-1790164799999` | ENTER_LONG | 2026-09-23T 12:03:23.493+00:00 | `8264672b-d0e0-48f2-825a-386b30010913` |
| B | `clock-1h-1790189999999-119` / `ETH-USDT-1h-1790189999999` | ENTER_LONG | 2026-09-23T 19:04:11.824+00:00 | `ac91f5df-6aea-47cb-b325-b8a558386bc6` |
| B | `clock-1h-1790204399999-124` / `ETH-USDT-1h-1790204399999` | ENTER_LONG | 2026-09-23T 23:02:45.552+00:00 | `f4e3bf00-b330-40df-b7c1-b91d2a062c7f` |
| B | `clock-1h-1790261999999-145` / `ETH-USDT-1h-1790261999999` | ENTER_SHORT | 2026-09-24T 15:03:47.521+00:00 | `1683ea96-0479-4db2-b93b-0a2a5b3014dc` |
| B | `clock-1h-1790330399999-170` / `ETH-USDT-1h-1790330399999` | ENTER_LONG | 2026-09-25T 10:01:37.043+00:00 | `464c4c14-4675-424c-8478-38facad2ebee` |
| B | `clock-1h-1790341199999-174` / `ETH-USDT-1h-1790341199999` | ENTER_LONG | 2026-09-25T 13:02:26.277+00:00 | `a673ea1a-7df5-4985-9a47-d8c1b0dfb06d` |
| P | `clock-1h-1790456399999-215` / `ETH-USDT-1h-1790456399999` | ENTER_LONG | 2026-09-26T 21:01:44.079+00:00 | `efafde3a-2092-404a-a275-1705e94f156e` |
| P | `clock-1h-1790474399999-222` / `ETH-USDT-1h-1790474399999` | ENTER_LONG | 2026-09-27T 02:01:38.155+00:00 | `586b3162-2dac-479e-9a0f-4098369acc2e` |

## 7. Trader failure taxonomy

### Inventário completo e rastreabilidade

| Categoria efetiva do host | Baseline | Posterior | Classificação |
|---|---:|---:|---|
| Alias `15m` em source onde host exige `M15` | 11 | 11 | Representação/contrato; não candle H1/H4/D1 |
| JSON inválido ou protocolo textual de tools inválido | 8 | 11 | Serialização/tool calling |
| Schema / keys proibidas | 4 | 4 | Contrato; uma colisão semântica com a palavra `position` |
| Source não corresponde ao OHLC fechado citado | 2 | 4 | Coordenadas de referência; não comprovada fabricação de preço |
| **Total de ciclos falhos** | **25** | **30** | Sem intent final aceito, antes do GM |

O inventário detalhado contém, **para cada falha**, mensagem final do host, schema error ou JSON parse error, tentativa, snapshot hash, tools efetivamente executadas, modelo, endpoint, hashes SYSTEM/user/response, timestamps e IDs do evento de failure: [failures.json](brooks_v2_investigation_evidence/failures.json). Os arquivos completos, sem resumir texto enviado/recebido, estão em [captures](brooks_v2_investigation_evidence/captures/) e [wire_requests](brooks_v2_investigation_evidence/wire_requests/), indexados com SHA256 em [capture_index.json](brooks_v2_investigation_evidence/capture_index.json).

Em um capture, `system` é o system prompt, `calls[0].user_message` é o packet/schema inicial completo, `calls[].raw_response` é o texto entregue pelo client, `calls[].user_message` subsequente contém results do host, e `tools[]` prova dispatch/status. No wire, `body.messages` registra a mensagem efetiva e o histórico no endpoint. **Não foi capturada a resposta HTTP integral**, usage, finish_reason ou raciocínio privado do modelo. O relatório analisa evidência expressa e chamadas observadas.

Todas as 30 falhas são `attempt=1`, `transient=false`; não receberam retry de reparo. A política automática é para falhas transitórias, não para protocolo/schema/OHLC. Há um reparo específico de cobertura de timeframes depois do parse; ele não cobre estas falhas. O wrapper `role_runs.status=completed` em parte delas significa que o consumer retornou; `cycle.status=failed` e o evento persistido provam a ausência do intent aceito.

| Corte | Round | Categoria | Diagnóstico individual | Capture completo |
|---|---|---|---|---|
| B | `clock-1h-1789869599999-5` | Schema | $: Value error, private or future market field: position | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1789869599999-5-1-71be0235.json) |
| B | `clock-1h-1789873199999-6` | Alias M15 | JSON parses and TradeIntentV2 accepts it; both reference timeframes use 15m where host requires literal M15. Both OHLC refs otherwise exactly match the initial 120-bar M15 window. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1789873199999-6-1-66c39a10.json) |
| B | `clock-1h-1789891199999-12` | JSON/tools | two JSON tool requests joined by </br> | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1789891199999-12-1-5a455463.json) |
| B | `clock-1h-1789923599999-24` | Alias M15 | JSON parses and TradeIntentV2 accepts it; both reference timeframes use 15m where host requires literal M15. Both OHLC refs otherwise exactly match the initial 120-bar M15 window. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1789923599999-24-1-d40cb9de.json) |
| B | `clock-1h-1789927199999-25` | Source OHLC | OHLC/timestamp/index audit below shows which reference field(s) fail; no conclusion that a price was fabricated. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1789927199999-25-1-217fb766.json) |
| B | `clock-1h-1789952399999-35` | Alias M15 | JSON parses and TradeIntentV2 accepts it; both reference timeframes use 15m where host requires literal M15. Both OHLC refs otherwise exactly match the initial 120-bar M15 window. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1789952399999-35-1-62966413.json) |
| B | `clock-1h-1789981199999-45` | Alias M15 | JSON parses and TradeIntentV2 accepts it; both reference timeframes use 15m where host requires literal M15. Both OHLC refs otherwise exactly match the initial 120-bar M15 window. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1789981199999-45-1-670f52c2.json) |
| B | `clock-1h-1790002799999-52` | JSON/tools | truncated or unbalanced final JSON; terminal delimiter does not close expected structure | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790002799999-52-1-25ff5646.json) |
| B | `clock-1h-1790013599999-56` | Alias M15 | JSON parses and TradeIntentV2 accepts it; both reference timeframes use 15m where host requires literal M15. Both OHLC refs otherwise exactly match the initial 120-bar M15 window. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790013599999-56-1-e46693b7.json) |
| B | `clock-1h-1790017199999-57` | JSON/tools | JSON tool request mixed with tool-result-like markup not recorded as a host result | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790017199999-57-1-855abf89.json) |
| B | `clock-1h-1790020799999-58` | Alias M15 | JSON parses and TradeIntentV2 accepts it; both reference timeframes use 15m where host requires literal M15. Both OHLC refs otherwise exactly match the initial 120-bar M15 window. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790020799999-58-1-5c87b911.json) |
| B | `clock-1h-1790024399999-60` | Alias M15 | JSON parses and TradeIntentV2 accepts it; both reference timeframes use 15m where host requires literal M15. Both OHLC refs otherwise exactly match the initial 120-bar M15 window. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790024399999-60-1-268f2397.json) |
| B | `clock-1h-1790031599999-62` | Alias M15 | JSON parses and TradeIntentV2 accepts it; both reference timeframes use 15m where host requires literal M15. Both OHLC refs otherwise exactly match the initial 120-bar M15 window. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790031599999-62-1-d3cf5843.json) |
| B | `clock-1h-1790049599999-69` | JSON/tools | JSON protocol mixed with or replaced by DSML tool-call markup | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790049599999-69-1-42f5f2a5.json) |
| B | `clock-1h-1790056799999-72` | Alias M15 | JSON parses and TradeIntentV2 accepts it; both reference timeframes use 15m where host requires literal M15. Both OHLC refs otherwise exactly match the initial 120-bar M15 window. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790056799999-72-1-ec267b07.json) |
| B | `clock-1h-1790067599999-76` | Schema | evidence_against: Input should be a valid list | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790067599999-76-1-24b85c47.json) |
| B | `clock-1h-1790071199999-77` | Schema | evidence_against: Input should be a valid list | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790071199999-77-1-85d78842.json) |
| B | `clock-1h-1790078399999-79` | JSON/tools | JSON protocol mixed with or replaced by DSML tool-call markup | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790078399999-79-1-ca288775.json) |
| B | `clock-1h-1790107199999-89` | JSON/tools | JSON protocol mixed with or replaced by DSML tool-call markup | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790107199999-89-1-d537ee75.json) |
| B | `clock-1h-1790200799999-123` | Alias M15 | JSON parses and TradeIntentV2 accepts it; both reference timeframes use 15m where host requires literal M15. Both OHLC refs otherwise exactly match the initial 120-bar M15 window. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790200799999-123-1-9184c2a3.json) |
| B | `clock-1h-1790207999999-125` | Source OHLC | OHLC/timestamp/index audit below shows which reference field(s) fail; no conclusion that a price was fabricated. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790207999999-125-1-6a1dedf1.json) |
| B | `clock-1h-1790233199999-135` | Schema | evidence_against: Input should be a valid list; evidence_against_extra: Extra inputs are not permitted | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790233199999-135-1-48d3450c.json) |
| B | `clock-1h-1790236799999-136` | JSON/tools | JSON tool request mixed with tool-result-like markup not recorded as a host result | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790236799999-136-1-c1179384.json) |
| B | `clock-1h-1790243999999-139` | Alias M15 | JSON parses and TradeIntentV2 accepts it; both reference timeframes use 15m where host requires literal M15. There is also a latent raw OHLC exact-text/reference mismatch; see per-reference audit. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790243999999-139-1-7fc00e1a.json) |
| B | `clock-1h-1790337599999-172` | JSON/tools | malformed final JSON syntax | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790337599999-172-1-38943ef4.json) |
| P | `clock-1h-1790405999999-197` | JSON/tools | final JSON contains a single-quoted string delimiter | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790405999999-197-1-2f00e7b7.json) |
| P | `clock-1h-1790409599999-198` | JSON/tools | JSON protocol mixed with or replaced by DSML tool-call markup | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790409599999-198-1-52005c60.json) |
| P | `clock-1h-1790488799999-227` | Source OHLC | OHLC/timestamp/index audit below shows which reference field(s) fail; no conclusion that a price was fabricated. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790488799999-227-1-be14ce92.json) |
| P | `clock-1h-1790495999999-229` | JSON/tools | JSON protocol mixed with or replaced by DSML tool-call markup | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790495999999-229-1-ef03101e.json) |
| P | `clock-1h-1790517599999-237` | Source OHLC | OHLC/timestamp/index audit below shows which reference field(s) fail; no conclusion that a price was fabricated. | [JSON](brooks_v2_investigation_evidence/captures/clock-1h-1790517599999-237-1-3dfd5e8f.json) |

### Alias M15: a ambiguidade é demonstrável

O runtime pede sources M15, mas a ferramenta usa `timeframe="15m"`; o JSON schema de `TriggerSource.timeframe` diz apenas `type: string`, sem enum. O host exige a grafia literal `M15`. Os 11 outputs usaram `15m` em ambas as sources. Nos dez primeiros, todas as coordenadas e strings de preço passariam o teste da janela M15 se só a grafia fosse canonicalizada. O último, round 139, também cita low `2638.00` onde raw é `2638`; equivalência numérica não satisfaz a comparação de strings atual. Não alterei ou flexibilizei essa regra.

Exemplo real, round 6: `"source":{"timeframe":"15m",...}` para trigger e invalidation, com `decision_timeframe:"M15"`. Host: `production entry trigger and invalidation must cite M15 bars`. [Resposta e prompt completos](brooks_v2_investigation_evidence/captures/clock-1h-1789873199999-6-1-66c39a10.json).

### JSON e tools: 8 pedidos não chegaram a executar

- Round 12 respondeu **dois objetos** de tool separados por `</br>`: primeiro `get_closed_candles(4h,120)`, depois `get_recent_structure(15m,30)`. O parser exige um objeto. Não executou nenhum.
- Rounds 69, 79, 89, 198 e 229 misturam/substituem JSON por markup `DSML invoke`. Também foram recusados antes do dispatch.
- Round 57 acrescenta `<result>` com barras aparentes à sua própria tool request. Round 136 acrescenta `<system>Tool result...`. Esses resultados **não foram fornecidos pelo host** nem reconhecidos como uma consulta real.
- Round 52 tem JSON final desbalanceado; round 172 tem JSON final malformado depois de uma consulta H4 válida; round 197 usa aspas simples numa string de array. Sem finish_reason não atribuo o round 52 a truncamento do provider.

O SYSTEM explicita JSON exclusivo e proíbe XML/DSML. A falha não é falta dessa frase. O envelope efetivo, porém, não fornece tools nativas ou enforcement de schema ao provider. O texto exposto pode refletir o modelo ou a adaptação do provider; capturas de request/texto sem HTTP response completo e sem backend de controle não discriminam essa origem.

### Schema: distinguir estrutura de segurança de conteúdo

Round 5 descreve `market_context.m15_facts.position` como **posição do preço dentro do range**, sem estado de conta. A guarda recursiva de campos privados rejeita a chave `position` mesmo nesse sentido de mercado. É uma colisão entre o dict narrativo aberto e o vocabulário de segurança. Rounds 76/77 enviam `evidence_against` como string, não lista; round 135 repete isso e adiciona `evidence_against_extra`, proibido. A recusa é correta segundo o contrato atual; não prova erro de leitura do mercado.

### OHLC: preços reais com coordenadas inconsistentes

| Round | Campo | Output | Raw correto | Diagnóstico |
|---|---|---|---|---|
| 25 | invalidation | idx 118, open `1789924500000`, close `1789925399999`, low `2624.89` | idx 118 open `1789925400000`, close `1789926299999`, mesmo low | Usou horários do idx 117 com preço/índice do 118; trigger exato |
| 125 | invalidation | idx 112, close `1790201799999`, low `2675.73` | close `1790201699999`, mesmo open/idx/low | Close +100.000 ms; trigger exato |
| 227 | trigger e invalidation | idx 118, close `1790487999999`, high `2707.7`, low `2694.44` | close `1790487899999`, mesmos open/idx/preços | Close +100.000 ms em ambas |
| 237 | trigger | idx 96, open `1790496000000`, close `1790498699999`, high `2718.83` | idx 96 close `1790496899999`, mesmo high | Close do idx 98; high repetido nos 96/98. Invalidation exata |

Nenhum desses quatro fez consulta M15/H1. Portanto uma limitação adicional do monitor de tools — não registrar novas janelas M15 menores, apenas H4/D1 — é **exposição potencial**, não causa desses exemplos. Esses preços existem no raw; a falha é atribuir a referência completa correta. Não há prova de nível inventado/interpolado nesse conjunto.

### Erros de mercado, de prompt e de modelo

As categorias acima cobrem todas as falhas técnicas, sem rotulá-las como raciocínio de mercado ruim. A leitura de mercado é avaliada nas seções 8/11 com limitações. Há ambiguidade demonstrada no namespace `15m/M15` e na chave `position`; a associação entre densidade do packet e cópia de timestamps é plausível, mas precisa de experimento. Todos usam o mesmo alias/modelo; não há base para declarar defeito específico da família DeepSeek ou superioridade de outro modelo.

### Retry e falhas de transporte: exposição independente

Há um caminho compartilhado não identificado como causa destas 30 falhas: `PydanticAIClient.prompt_stream()` transforma timeout em `PromptDone(timeout)` sem texto e outras exceções em texto de erro + `PromptDone(error)`. `prompt()` junta somente `TextChunk` e descarta `PromptDone`. O runner pode receber vazio/texto de erro, classificá-lo como JSON inválido não transitório e deixar de aplicar o retry esperado. A inspeção e stub do agregador/parser sustentam a exposição; os failures do lote têm texto não vazio e nenhuma exceção capturada nessa fronteira. Não os reclassifico retroativamente como timeout.

## 8. Skill Brooks evaluation

### Método de comparação

Li integralmente os três diretórios: `SKILL.md`, `RUNTIME.md` quando existe e todos os `references/*`. O PM não tem RUNTIME separado e recebe a Skill integral. Trader recebe entry runtime; Context Analyst recebe context runtime. Não há contrato standalone de contexto concorrente no system do Trader. Os runtimes capturados correspondem ao checkout; a avaliação inclui o texto real, não apenas documentação.

O núcleo é consistente com a fonte primária: setup combina sinal e contexto; formato de uma barra sozinho não determina a entrada. O material oficial também distingue signal/entry bar e stop entry além do extremo, e define Always-In como leitura estrutural, não garantia de próximo movimento. [Al Brooks: candlestick charts](https://www.brookstradingcourse.com/how-to-trade-manual/candlestick-charts/), [stop orders](https://www.brookstradingcourse.com/how-to-trade-manual/stop-orders/), [glossary](https://www.brookstradingcourse.com/price-action-trading-terms-glossary/). Isto fundamenta a comparação conceitual, não substitui uma revisão independente dos gráficos.

### O que a Skill pede versus o observado

| Conceito | Instrução vigente | Comportamento observado / limite |
|---|---|---|
| Trend vs range | Regime, fase e pressão separados; ler overlap, tails, swings, closes | Contexto H4 em `1790179199999` conserva bull-trend com phase range, pressão bear e Always-In short baixo. Eixos distintos usados; não há gold labels para dizer se regime é o melhor |
| Always-In | Influência contextual, especialmente baixa em range; não é ordem | Em conflito D1 long/H4 short há NO_TRADE e entradas para ambos os lados; não existe voto automático demonstrado |
| Breakout | Tentativa além do limite, acceptance/follow-through só se observado | Round 222 chama tentativa de breakout e mantém pending; não inventa barra futura de confirmação |
| Breakout pullback | Avaliar retorno, profundidade, overlap e oposição | Round 7 cita breakdown idx 115, bounce idx 116 e flag (a frase de retorno ao range é contraditória, ver R2); diferencia contexto D1 bull de setup short |
| Failed breakout | Exigir retorno/rejeição observável, não apenas falta de trigger | Round 215 cita varredura abaixo do coil low e close de volta dentro; hipótese bear trap tem fonte observável |
| Reversal / MTR | Teste e mudança estrutural; `mtr-like` não confirma reversão | Round 119 nomeia double bottom/second entry com oposição H4/H1 explícita. Não foi validada manualmente cada contagem High 2/Low 2/MTR do lote |
| Climax | Extensão, spike, perda de follow-through; não reversão automática | Contextos pós spike 2850 contrapõem dominante perna bull e overlap posterior. Rotular climax continua interpretativo |
| Location | Bordas, breakout point, extensão, obstáculos; meio de range desfavorável | 93/134 NO_TRADE são poor_location; também há 17/25 ENTER com location neutral. Critério tem margem de julgamento e pode ser inconsistente entre casos |
| Signal bar | Corpo/close/tails e contexto à esquerda; padrão não basta | Amostras citam OHLC e contraponto; weak signal aparece numa entrada round 215, exigindo justificar compensação contextual |
| Trigger | Source M15 fechado, pending distinto de triggered/fill | 23/25 ENTER são pending,1 present,1 triggered. Contrato diferencia; execução MARKET é outro componente |
| Invalidation | OHLC estrutural M15 exato, não preço fabricado | 25 intents aceitos passam;4 proposals falham coordenadas. Campo source protege execução, não valida sozinho a qualidade estrutural |
| Evidence for/against | Observável, citado, strongest opposite case | Auditoria lexical:1.051/1.231 itens de evidência aceitos têm marcador de barra/horário (85,4%). Os 180 restantes podem ser contexto geral; não equivalem a alucinação |
| NO_TRADE | Abstinência por causa específica, sem exigir confirmação futura indevida | Taxa válida 80,8% baseline/84,3% posterior; padrões poor_location/balanced predominam. Sem labels de oportunidades não determina excesso/insuficiência |

O contexto posterior tem **57 role outputs concluídos, 56 contextos/tempos únicos**:9 D1 e 47 H4 incluindo bootstrap. Um retry H4 em `1790150399999` completou novamente. Não são 57 classificações independentes. Todos os 542 limites auditados correspondem numericamente a um OHLC fornecido;27 variam só formato decimal. Isso não garante narrativa/classificação corretas: o consumer valida identidade/tempo/janela, não cada frase ou bound como uma source estruturada.

### Aderência útil e fragilidades

As melhores respostas conectam regime, localização, barra de sinal e oposição, em vez de só citar “Brooks”. A entrada short contra D1 bull e a reversão long sob H4 short são evidência contra majority voting. D1 permaneceu bull/Always-In long no corte; isso pode ser coerente com a janela e não demonstra cegueira direcional sem anotação independente.

Há frases recorrentes sobre overlap, borda de range e falta de follow-through. Repetição pode refletir o mesmo mercado; não é automaticamente texto vazio. Algumas condições futuras são genéricas e não especificam qual teste diferencia setup ativo de hipótese. A Skill define as distinções, mas sua observância completa não é machine checked. A contagem High 1/High 2/Low 1/Low 2 e a confirmação de MTR merecem revisão humana cega com anchors; o lote não oferece ground truth.

Reference loading é raro: Trader 5 calls baseline/7 posterior, sempre `trade_entry.entry_evidence`; Context 1 `market_context.context_evidence`; PM 0. Nenhuma source_notes, nenhum reference contextual no Trader. Os cinco arquivos cabem no limite de 12.000 caracteres; não há truncamento do reader. As cinco leituras em ciclos válidos terminaram NO_TRADE, mas seleção por ambiguidade confunde a comparação: não prova que references causam abstinência ou são inúteis.

O PM não segue a orientação condicional de carregar management_evidence quando avalia premissa/oposição em vários HOLDs. Porém já recebe a Skill completa e demonstra checklist; esse desvio de tool-use não prova desconhecimento. Mais relevante: a Skill pede reconciliação de custo/fill/barreira que o input não entrega. Um prompt não recupera fatos removidos pelo adapter.

## 9. TradeIntentV2/output evaluation

### Campos que protegem e ajudam a auditoria

`decision`, mecanismo e trigger_status distinguem ausência de setup, stop pendente e condição ativa. Sources com timeframe, índice, open/close, field e preço tornam o nível verificável. Evidence for/against e location permitem inspecionar a justificativa. `extra=forbid`, enums e host gates evitam que saída malformada se torne write. A proteção funcionou: failures recusados não chegaram ao GM.

### Superfície que adiciona esforço sem validação equivalente

- `market_context: dict[str,Any]` aceita narrativa arbitrária e duplicação dos macro contexts. Isso amplia o output e permite keys que colidem com a guarda de privacidade (`position` real no round 5).
- `setup.type`, texto `reference` e arrays de evidência/uncertainty/conditions são livres. O host não confirma que um double bottom, High 2 ou room to resistance existe porque o rótulo foi declarado.
- Arrays narrativos obrigatórios não vazios e sem limite de tamanho podem induzir justificativas extensas/artificiais; a ocorrência de repetição é observada, o efeito causal do requisito não foi medido.
- `qualitative_confidence` é auditável como opinião, mas não calibrada e não altera o GM. Não mede probabilidade ou previsão.
- `decision_timeframe` duplica parte das sources; o requisito exato M15 não aparece como enum no schema de source. `context_timeframes_used` é autodeclaração, não substitui auditoria de tools.
- Repetir índice, dois timestamps e preço exige cópia consistente de quatro coordenadas. A checagem é útil; pedir que o modelo produza todos cria uma superfície de erro demonstrada. Não avalio relaxar o validator nesta rodada.

A conclusão é localizada: **o contrato protege writes melhor do que mede qualidade de decisão**. Vários campos tornam a resposta legível, mas não comprovam acerto de regime/setup. Não há evidência para remover contraponto ou transformar output em previsão de preço.

### PydanticAI não significa enforcement nativo neste wiring

`run_role()` concatena `output_model.model_json_schema()` no user. `PydanticAIClient.start()` cria Agent sem output_type; todos os wire bodies têm apenas `messages/model/stream`, sem `response_format/json_schema/tools/tool_choice`. O host faz parse e Pydantic depois. JSON/texto customizado de tool é outra saída concorrente em cada turno, embora só haja um contrato final. A enumeração explícita na mensagem não constrange sintaxe no provider. Isto explica a fronteira onde os erros são recusados; não prova que usar outro modo resolveria todos.

## 10. Model decision/prediction limitations

| Dimensão | Evidência | Limitação / hipótese testável |
|---|---|---|
| Informação inicial |120 H1 +120 M15 raw; D1/H4 resumidos | M15 cobre 30h e H1 cinco dias. Macro details/testes que o analista omite só chegam se Trader pedir raw. Sem ground truth não há prova de janela insuficiente |
| Compressão contextual | D1/H4 guardam leitura e estruturas, não todos os OHLC | Pode preservar direção e perder sequência de testes/contexto à esquerda.56 tempos únicos têm bounds reais, mas classificação não validada independentemente |
| Freshness | H4 stale 54/189; D1 stale 8/189; sem missing | Os sete ENTER H4 stale leram 120 raw H4. D1 stale aparece usado num ENTER sem raw D1: materialidade não estabelecida |
| Cadência | DisparoH 1, quatro M15 por ciclo | Sinais podem aparecer/inativar entre H1 e execução. Possibilidade exige replay comparativo; nenhum “trade perdido” comprovado por hindsight |
| Densidade/ordem | SYSTEM 7.042 chars, primeiro USER 55.491–60.039; schema/allowlist antes do packet | Raw H1/M15 dominam volume; instruções distantes e timestamps sem índice explícito podem aumentar cópia/atenção. Caracteres não são tokens e não provam overflow |
| Ferramentas |4 tools do Trader; estrutura só min/max/lastclose,volatility média de high-low | Não oferecem swing annotations/contagem de padrão; structure reutiliza informação das barras. Nenhum get_volatility, nenhum raw D1 neste corte |
| Referências |7 Trader reads;5 em ciclos válidos | Ambiguidade/materialidade para ler é subjetiva. Comparação causal exige mesmos packets com/sem reference |
| Structured output | Schema textual, tool JSON textual | Falhas de protocolo e shape ocorrem apesar de SYSTEM claro; interface não usa enforcement nativo |
| Modelo | Único alias DeepSeek no endpoint OpenCode Go | Sem controle mesma entrada/modelos diferentes nem raw HTTP response não dá para separar capacidade, protocolo e serializer |
| Latência | Turno do Trader: mediana 108,994s baseline/98,749s posterior; máximo 285,572s | São calls, não duração total com tools/retry. Frozen snapshot íntegro; oportunidade pode mudar na venue antes de write, guardas posteriores não foram avaliadas após o bloqueio de binding |
| PM input/history |SYSTEM 16.277 chars;USER 12.939–91.952, med 86.000; últimos 10 decisions≈79k num exemplo | Crescimento é real e pode dificultar leitura, mas não prova causa dos 7 failures. Entry/fills/barriers removidos impõem limitação factual certa |

Não há volume profile/order-flow obrigatório em Brooks nem na Skill. Esses dados não são necessários para reproduzir o método declarado. A avaliação deste lote não justifica acrescentá-los para satisfazer desejos genéricos do modelo. A prioridade investigativa é distinguir informação de preço relevante que foi perdida de formato/protocolo que atrapalha seu uso.

A capacidade preditiva **não está medida** por justificativas convincentes, taxa ENTER ou um stop. Para saber se as limitações afetam decisões seria preciso comparar inputs/output modes nos mesmos packets, julgados sem candles futuros, antes de medir outcomes num fluxo com lifecycle íntegro. Nenhuma dessas mudanças/experiências com LLM foi feita aqui.

## 11. Representative decision reviews

Estas revisões são **ex-ante**: somente raw/context/tool results com close ≤ cutoff, e a saída disponível naquele momento. Os IDs abaixo localizam os captures completos; [decision_reviews.json](brooks_v2_investigation_evidence/decision_reviews.json) conserva 21 revisões focais. Não há nota de acerto baseada nos candles seguintes.

### R1 — NO_TRADE em range, H4 stale atualizado

`clock-1h-1789862399999-1`: última M15 idx 119, O 2633.91/H 2634.63/L 2632.98/C 2632.98. Piso 2604.75 e teto 2658.79 vêm das janelas disponíveis. Há leitura alternativa de bull flag, mas a localização no interior enfraquece a entrada; lê 120 H4 raw e marca essa evidência acima da interpretação stale. É abstinência alinhada à Skill, sem hipótese de lucro posterior. [Capture](brooks_v2_investigation_evidence/captures/clock-1h-1789862399999-1-1-a03fb335.json).

### R2 — ENTER_SHORT após breakdown, contra D1 bull

`clock-1h-1789876799999-7`: M15 idx 115 O 2603.75/H 2606.04/L 2571/C 2573.13 rompe piso 2604.75;116–119 sobrepõem 2569.8–2594.57. Trigger abaixo de low 2569.8 e invalidação high 2594.57, ambos idx 117, exatos. Lê 120 H4 raw por stale. D1 bull e compra na última barra entram no contraponto, não vetam automaticamente.

Há **um erro narrativo observável**: evidence_against diz que idx 116 “closed back up inside the broken range”. Seu close 2579.45 continua abaixo de 2604.75; houve bounce dentro da flag posterior, não recuperação daquele range anterior. O mesmo output elsewhere afirma que a flag não reclamou 2604.75. A hipótese short tem evidência concreta, mas texto aceito não é integralmente factual. Validação das sources não detecta essa contradição. [Capture](brooks_v2_investigation_evidence/captures/clock-1h-1789876799999-7-1-31b6e86d.json).

**Separação ex-post:** este intent deu origem à única posição simulada e ao STOP. O fill MARKET 2583.861588 ocorreu antes de seu stop de entrada abaixo 2569.8 ser demonstrado como disparado; isso impede tratar seu resultado como teste limpo daquela proposta pending. Não usei o STOP para rejeitar a leitura ex-ante.

### R3 — ENTER_LONG com location neutral e resistência próxima

`clock-1h-1789916399999-21`: M15 idx 119 O 2581.25/H 2586/L 2579.33/C 2586; trigger high 2586, invalidação low 2568.16 no idx 105. Output declara location neutral e supply 2594.57 apenas 8.57 acima, além de alternativa bear flag/D1 mid-range. A estrutura/contraponto é explícita, mas o critério de espaço versus invalidation de 17.84p merece julgamento independente. Não afirmo entrada inválida segundo a Skill, que admite compensação contextual qualitativa. [Capture](brooks_v2_investigation_evidence/captures/clock-1h-1789916399999-21-1-a9431f09.json).

### R4 — ENTER_SHORT em falha de topo, mas suporte imediatamente abaixo

`clock-1h-1789955999999-36`: idx 119 O 2677.50/H 2682.68/L 2648.28/C 2648.42; stop abaixo 2648.28, invalidation high 2700 no idx 113. Há top test/double top e bear signal close on low. O próprio modelo contrapõe suporte 2644.86/2641.70 só 3,42/6,58 abaixo do trigger e risco estrutural 51,72 até 2700. A favorable location no topo macro compete com pouco espaço imediato. É uma amostra **questionável pela própria Trader's Equation qualitativa**, sem dizer que deveria obrigatoriamente NO_TRADE ou usar outcome. Sources exatas e GM bloqueado por binding são fatos separados. [Capture selecionado no índice](brooks_v2_investigation_evidence/capture_index.json).

### R5 — NO_TRADE com referência e dois lados inviáveis por motivos diferentes

`clock-1h-1790053199999-71` lê `trade_entry.entry_evidence`. Última M15 O 2729.40/H 2731.24/L 2725.13/C 2727.37 é bear perto do low; reversal bull idx 113 já perdeu seu low 2727.22 dentro da janela. Long depende de sinal melhor; short fica perto de suporte 2718.09 enquanto invalidação candidata 2741.20 está mais distante. NO_TRADE weak_signal tem explicação ligada aos candles, não simples empate de votos. A presença de referência não prova causalidade, mas aqui checklist contextual se materializa no output. [Capture](brooks_v2_investigation_evidence/capture_index.json).

### R6 — D1 stale sem leitura raw; materialidade incerta

`clock-1h-1790035199999-63`: ENTER_LONG com H4 stale verificado via 120 raw e D1 stale incluído em context_timeframes_used sem raw D1. Não infringe o gate deterministicamente exigido paraH 4. Falta saber se D1 stale determinou o lado ou só localizou a estrutura; M15/H1/H4 fornecem fontes da proposta. O registro não basta para inferir voto ou hindsight. [Capture](brooks_v2_investigation_evidence/captures/clock-1h-1790035199999-63-1-e8d33d2f.json).

### R7 — Reversão long sob conflito D1/H4

`clock-1h-1790189999999-119`: D1 Always-In long, H4 Always-In short/pressão bear. M15 idx 108 low 2641.37 fecha 2662.39 perto de high 2662.52; idx 113 higher low 2649.16, idx 119 high 2672.55 dá trigger pending. A oposição cita H4 bear 79,28p, bounce pequeno, lower highsH 1 e resistência 2681.07 só 8,52 acima do trigger com risco 31,18. Reconhece conflito e não usa veto/voto automático; ainda é uma entrada neutral com room questionável. Double bottom/second entry exige validar contagem, não basta o nome. [Capture](brooks_v2_investigation_evidence/captures/clock-1h-1790189999999-119-1-cc516b38.json).

### R8 — Failed bear breakout/bear trap, sinal weak

`clock-1h-1790456399999-215`: última M15 O 2670.85/H 2676.05/L 2665.14/C 2675.70 varre coil low 2669.67 e fecha dentro, perto do high. Trigger 2676.05 e invalidation 2665.14 exatos. O contexto favorece teste de borda, mas seis lower highsH 1 e falta de confirmação contrariam. ENTER pending apesar de signal_quality weak é permitido em tese pela compensação de location favorable; isso precisa comparação consistente com casos NO_TRADE, não resultado posterior. [Capture](brooks_v2_investigation_evidence/captures/clock-1h-1790456399999-215-1-fc877240.json).

### R9 — Breakout pending e, em outro cutoff, pullback recusado

`clock-1h-1790474399999-222`: última M15 O 2694.18/H 2703.08/L 2693.97/C 2700.45 supera máximos recentes; trigger acima 2703.08, invalidation 2693.97. H4 continua range e o modelo reconhece resistência e ausência de follow-through; não inventa confirmação para decidir ENTER pending. [Capture](brooks_v2_investigation_evidence/captures/clock-1h-1790474399999-222-1-dddab941.json).

No **packet distinto** `clock-1h-1790477999999-223`, já há outros quatro M15 fechados. Modelo carrega entry_evidence e escolhe NO_TRADE após dois bear bars; low 2696.59 ainda está acima do breakout point 2696.09. O próprio contraponto define long acima 2699.61 com invalidation 2693.97 e admite breakout pullback defensável. A causa declarada é localização/qualidade, não simples pending. É um candidato útil para avaliação cega de conservadorismo. Não uso as barras 223 para julgar 222. [Capture no índice](brooks_v2_investigation_evidence/capture_index.json).

### R10 — NO_TRADE com referência, mas “última barra” errada

`clock-1h-1790398799999-195`: lê entry_evidence e usa range/overlap para NO_TRADE. O output chama de final M15 a barra open 1790394300000, close 2686.84, range 0,73; ela é **idx 115**, não 119. O último M15 real abre 1790397900000, O 2687.52/H 2688.9/L 2687.34/C 2688.33. A localização geral permanece range, mas o argumento específico de signal bar seleciona uma observação antiga. Isto comprova erro factual de atenção/atribuição numa decisão válida, sem provar ação incorreta. [Capture no índice](brooks_v2_investigation_evidence/capture_index.json), [auditoria manual](brooks_v2_investigation_evidence/manual_decision_audit.json).

### R11 — Context Analyst e references

H4 em `1790179199999` conserva regime bull, phase range, Always-In short/relevance low; H4 em `1790539199999` conserva trading-range, breakout_mode, pressão balanced/Always-In unclear. Eixos não viram ordem. Contexto em `1790251199999` carrega context_evidence por ambiguidade estrutural. Bounds numéricos observados são OHLC reais, mas regime/fase continuam interpretações falíveis. [Auditoria](brooks_v2_investigation_evidence/context_boundary_audit.json), [revisões](brooks_v2_investigation_evidence/decision_reviews.json).

### R12 — PM HOLD e lacunas de informação

PM `timer-pm-1789888500000-339` recebe MAIN short 0.478, mark 2574.95, margem SAFE, executor RUNNING, policy strategy_stop_required=false. Lê 30 M15, compara breakdown/overlap/oposição e escolhe HOLD. Reconhece ausência de entry/fills. Isso é coerente com o input; não demonstra reconciliação completa que não poderia fazer.

Este PM e `timer-pm-1789910100000-363` afirmam 12 HOLDs anteriores quando management_history tem 10. São slips factuais reais sobre input. Em outra resposta, “only discretionary exit” excede o que open_orders vazio prova: a barreira automática do executor estava oculta ao PM. Nenhum HOLD capturado pediu REQUEST_MARKET_ANALYSIS; seu AttributeError default é exposição independente. [Captures e requests](brooks_v2_investigation_evidence/capture_index.json).

**Resultados posteriores não pontuam esses reviews.** Não há contrafactuais executados para as propostas bloqueadas e nenhum julgamento ex-post sobre candidatos não negociados.

## 12. Findings ranked by severity

Severidade considera o impacto observado e a exposição potencial. “Produção” significa código/wiring compartilhado; nenhum incidente live foi confirmado nesta investigação. Os IDs abaixo consolidam os achados; os JSONs dos agentes mantêm seus IDs locais.

### LIF-01 — HIGH — Binding permanece ativo após MAIN encerrar

- **Evidence:** STOP, POSITION_CLOSED e 24 rejeições; [trace](brooks_v2_investigation_evidence/lifecycle_trace.json), [probes](brooks_v2_investigation_evidence/lifecycle_extended_probe.json).
- **Files/functions involved:** GM `execute_entry/reconcile_main/execute_management`; adapters `read_bindings/HummingbotAccountReader`; PositionWatcher; supervisor subscriptions.
- **Observed behavior:** venue flat, executor CLOSED e binding reconciled com MAIN ID; AccountSnapshot indica estrutura irresolvida e GM bloqueia.
- **Expected behavior:** ownership persistido deve reconhecer encerramento confirmado e separar registro histórico de posição aberta, mantendo fail-closed quando a verdade ainda é incerta.
- **Confidence:** alta, artefatos, inspeção e reprodução com componentes reais.
- **Production impact:** stop, TP, time limit, CLOSE ou fechamento externo podem bloquear novas entradas indefinidamente no mesmo wiring.

### LIF-02 — HIGH — MAIN flat com HEDGE aberta perde gestão

- **Evidence:** [orphan_hedge_remove_attempt](brooks_v2_investigation_evidence/lifecycle_extended_probe.json).
- **Files/functions involved:** PM context loader, account reader, GM management/hedge guards.
- **Observed behavior:** HEDGE 0.120 remanescente, PM None; chamada real GM REMOVE_HEDGE recusa `hedge structure unresolved: orphan_hedge`, zero writes na port falsa.
- **Expected behavior:** exposição remanescente confirmada deve ter responsabilidade explícita de lifecycle/gestão sem exigir MAIN fictício.
- **Confidence:** alta para o fluxo reproduzido; nenhum HEDGE existiu no trade do replay.
- **Production impact:** exposição financeira potencial sem caminho automático de gestão nesse estado.

### OUT-01 — HIGH — Alias `15m/M15` rejeita 11 propostas

- **Evidence:** [11 rows com sources e schema](brooks_v2_investigation_evidence/failures.json).
- **Files/functions involved:** entry runtime, TriggerSource, Trader reference validator, market tool timeframe protocol.
- **Observed behavior:** sources de barras M15 escritas como 15m; dez propostas têm restante da referência exato; a última também tem diferença decimal textual.
- **Expected behavior:** input, tool surface e schema devem comunicar sem ambiguidade a representação exata exigida.
- **Confidence:** alta; não são candles H1/H4/D1.
- **Production impact:** ciclos sem intenção no mesmo runtime/backend; host impede write com output inválido.

### OUT-02 — HIGH — JSON/tools textuais incompatíveis falham em 11 ciclos

- **Evidence:** [taxonomia e texto bruto](brooks_v2_investigation_evidence/failures.json), [wire requests](brooks_v2_investigation_evidence/wire_requests/).
- **Files/functions involved:** `agent_runner.run_role/_json_object`; Agent PydanticAI e transporte; wrapper SYSTEM.
- **Observed behavior:** DSML, result-like markup e serialização final inválida; oito pedidos não executam o primeiro read. Schema/tools não têm enforcement nativo na requisição.
- **Expected behavior:** protocolo consumível pelo host e failure explícito quando a geração não o cumpre.
- **Confidence:** alta sobre falha/envelope; média sobre qual camada emitiu o markup.
- **Production impact:** perda de disponibilidade de ciclos; zero intent fabricado. Causalidade específica do modelo não estabelecida.

### RUN-01 — HIGH — Erro transitório pode perder sua classificação no client

- **Evidence:** [provider_error_reproduction.json](brooks_v2_investigation_evidence/provider_error_reproduction.json), [script offline](brooks_v2_investigation_evidence/REPRODUCTIONS.md).
- **Files/functions involved:** PydanticAIClient `prompt_stream/prompt`; `_json_object`; `is_transient_role_error`.
- **Observed behavior:** PromptDone(timeout) vira texto vazio; PromptDone(error) fica como texto de erro. Ambos chegam ao parser como JSON inválido, transient=false no probe.
- **Expected behavior:** erro transitório deve chegar à política de retry com sua identidade preservada.
- **Confidence:** alta sobre a fronteira reproduzida; não é causa observada das 30 falhas deste lote.
- **Production impact:** timeout/transporte pode falhar sem retry Brooks nessa rota; failure continua persistido.

### SIM-02 — HIGH — Stop pending é executado como MARKET

- **Evidence:** [primeiro intent/fill/trace](brooks_v2_investigation_evidence/lifecycle_trace.json), [ledger](brooks_v2_investigation_evidence/posterior/simulation/).
- **Files/functions involved:** GM `compile_main/execute_entry`; ExecutionPort `open_main`; harness que reutiliza a port.
- **Observed behavior:** SHORT pending abaixo 2569.8 abre MARKET 2583.861588; risco/quantity do GM usam mark atual.
- **Expected behavior:** o teste deve representar o mecanismo decidido, ou explicitar que operacionaliza outra semântica.
- **Confidence:** alta, código e fill; nenhuma alteração feita.
- **Production impact:** port compartilhada usa MARKET. Afeta interpretação de entrada/risco/outcome; não causou as rejeições de ownership.

### PM-01 — HIGH — Input remove fatos necessários à reconciliação da Skill

- **Evidence:**47 captures PM, primeiro HOLD e [input audit](brooks_v2_investigation_evidence/input_audit.json).
- **Files/functions involved:** adapters `_pm_snapshot/_pm_sanitize_executor`; PM Skill e policy.
- **Observed behavior:** entry/custo/PnL individual ausentes; fills vazios; executor sem barriers. Modelo aponta lacunas e, num caso, extrapola open_orders vazio para “only discretionary exit”.
- **Expected behavior:** transmitir fatos disponíveis e distinguir stop order de barreira do executor; lacunas devem permanecer explícitas.
- **Confidence:** alta sobre omissões; efeito na action não estimado.
- **Production impact:** omissões do adapter são compartilhadas; disponibilidade dos fatos depende da API. O path bug SIM-01 é específico do harness.

### PM-02 — HIGH — Análise fresca default falha antes do LLM

- **Evidence:** [market_analysis_reproduction.json](brooks_v2_investigation_evidence/market_analysis_reproduction.json).
- **Files/functions involved:** `GMConsumer._build_default_analyst_runner` no supervisor; TraderMarketTools.
- **Observed behavior:** chamada a `as_tools()` inexistente produz AttributeError; provider e source não são chamados.
- **Expected behavior:** REQUEST_MARKET_ANALYSIS deve atingir a análise read-only ou registrar failure adequado, com interfaces existentes.
- **Confidence:** alta, execução offline; nenhum dos 40 HOLD acionou esse caminho.
- **Production impact:** rota default exposta quando PM solicita essa action; independente do STOP/binding observado.

### OUT-03 — MEDIUM — Preço real associado a coordenadas erradas

- **Evidence:**4OHLCrows e diferença decimal adicional em [failures.json](brooks_v2_investigation_evidence/failures.json).
- **Files/functions involved:** TriggerSource/PriceReference; Trader reference validator; arrays do packet.
- **Observed behavior:** horários de outro candle, close+100 segundos/+30minutos ou índice inconsistente; host recusa.
- **Expected behavior:** source completa coerente com uma janela observada e um candle fechado exato.
- **Confidence:** alta; nenhuma fabricação numérica comprovada nesses exemplos.
- **Production impact:** ciclo falha com proteção funcionando. Registro parcial de novas janelas M15 via tool é exposição adicional não exercitada.

### OUT-04 — MEDIUM — Narrativa válida contém erros factuais

- **Evidence:** R 2/R 10/R 12, [manual audit](brooks_v2_investigation_evidence/manual_decision_audit.json), [reviews](brooks_v2_investigation_evidence/decision_reviews.json).
- **Files/functions involved:** arrays narrativos TradeIntentV2/ManagementDecisionV2; modelo e input textual.
- **Observed behavior:** close abaixo do piso chamado de retorno ao range; idx 115 chamado último M15;12 HOLDs para array de 10; timestamp futuro na uncertainty sem candle futuro fornecido.
- **Expected behavior:** observações/contagens ligadas ao input correto e interpretação separada de fato.
- **Confidence:** alta nos exemplos; não foi verificada cada frase de toda a população.
- **Production impact:** guards não conferem toda narrativa. Pode afetar decisão/revisão; ação errada não é comprovada pelo outcome.

### SIM-01 — MEDIUM — Raiz PM no harness omite latest intent/context

- **Evidence:**47inputs null e [pm_root_reproduction.json](brooks_v2_investigation_evidence/pm_root_reproduction.json).
- **Files/functions involved:** WalkForward PM loader argument; `_pm_latest_intent/_pm_latest_context`.
- **Observed behavior:** dupla camada `brooks_state/brooks_state` resulta em None apesar dos dados persistidos.
- **Expected behavior:** replay consome os mesmos dados ambientais que o wiring de produção.
- **Confidence:** alta, fixture com ambas as raízes.
- **Production impact:** produção passa strategy_home correto; contaminação específica da avaliação PM no replay.

### LIF-03 — MEDIUM — Restart flat não reemite fechamento

- **Evidence:** [lifecycle_probe.json](brooks_v2_investigation_evidence/lifecycle_probe.json).
- **Files/functions involved:** supervisor inicializa PositionWatcher sem initial_snapshots; first poll; binding persistido.
- **Observed behavior:** watcher novo não tem transição anterior para publicar POSITION_CLOSED; binding continua ativo.
- **Expected behavior:** recovery do ownership deve funcionar mesmo quando o fechamento ocorreu antes do restart.
- **Confidence:** alta, probe; variante de LIF-01.
- **Production impact:** restart não corrige automaticamente o binding.

### SKL-01 — MEDIUM, hipótese — Critério qualitativo de location pode ser inconsistente

- **Evidence:** R 3/R 4/R 7 versus R 5/R 9;17 ENTERneutral;93 NO_TRADEpoor_location.
- **Files/functions involved:** entry runtime/evidence; setup/location/confidence; modelo.
- **Observed behavior:** entradas com resistência próxima coexistem com abstinência que reconhece setup defensável; falta ground truth para arbitrar.
- **Expected behavior:** critério consistente em situações comparáveis, com contraponto factual.
- **Confidence:** média sobre a tensão; baixa para declarar conservadorismo excessivo ou limitação preditiva.
- **Production impact:** possível instabilidade de seleção; exige experimento ex-ante.

### IN-01 — MEDIUM, hipótese — Densidade do input e crescimento de history

- **Evidence:** [input_audit.json](brooks_v2_investigation_evidence/input_audit.json), Trader USER 55–60kchars, PM USER até 91.952 chars.
- **Files/functions involved:** packet builder, ordenação de prompt, PM history loader/contract.
- **Observed behavior:** muito OHLC/timestamp e narrativa; últimos 10 outputs PM representam≈79kchars num exemplo.
- **Expected behavior:** uso confiável dos fatos/referências sem perda por formato ou duplicação.
- **Confidence:** alta sobre tamanho; baixa sobre causa de erros. Sem usage/finish_reason não há prova de overflow.
- **Production impact:** custo, latência e atenção possivelmente afetados; efeito requer A/B com snapshot igual.

### DATA-01 — LOW — Retry contextual completa novamente o mesmo tempo

- **Evidence:**57 outputs aceitos/56 tempos únicos; H4`1790150399999`; [context audit](brooks_v2_investigation_evidence/context_boundary_audit.json), captures selecionados.
- **Files/functions involved:** context retry/capture e context store/history.
- **Observed behavior:** dois outputs concluídos para um mesmo contexto lógico; contá-los como observações independentes inflaria a amostra.
- **Expected behavior:** métricas distinguem tentativa, output e contexto lógico único.
- **Confidence:** alta sobre capturas; nenhum Trader intent duplicado.
- **Production impact:** proveniência/contabilidade contextual; não causa ownership block nem demonstra concorrência de LLM.

## 13. Evidence matrix

| Pergunta / conclusão | Evidência primária | Arquivos/funções |
|---|---|---|
| Versão e fatos dos cortes | [manifest](brooks_v2_investigation_evidence/manifest.json), baseline git 7760041a; [cycles](brooks_v2_investigation_evidence/posterior/cycles.jsonl), [events](brooks_v2_investigation_evidence/posterior/events.jsonl) | Source a83; diff operativo registrado |
| Primeiro trade, STOP e binding | [trace](brooks_v2_investigation_evidence/lifecycle_trace.json), [binding](brooks_v2_investigation_evidence/posterior/binding.json), [ledger](brooks_v2_investigation_evidence/posterior/simulation/) | `_close_position`, GM/store |
| Quem escreve/observa lifecycle | [GM](../condor/brooks/gm.py), [adapters](../condor/brooks/adapters.py), [watcher](../condor/brooks/position_watcher.py), [supervisor](../condor/brooks/supervisor.py), [contracts](../condor/brooks/contracts.py) | Entry/reconcile/management/subscriptions |
| Produção, PM CLOSE e orphan HEDGE | [extended probe](brooks_v2_investigation_evidence/lifecycle_extended_probe.json), [diagnóstico documentado](brooks_v2_investigation_evidence/REPRODUCTIONS.md) | GM/readers reais, venue/port falsas |
| Cada ENTER rejeitado | [blocked_entries](brooks_v2_investigation_evidence/blocked_entries.json), IDs na seção 6 | `compile_main`/structure guard |
| Cada Trader failure, packet e texto | [taxonomy](brooks_v2_investigation_evidence/failures.json), [full captures](brooks_v2_investigation_evidence/captures/), [wire requests](brooks_v2_investigation_evidence/wire_requests/) | Runner/schema/reference gate |
| SYSTEM, user e history efetivos | `system/calls[].user_message/raw_response/tools`; wire body.messages | [Index/SHA256](brooks_v2_investigation_evidence/capture_index.json) |
| Retry, freshness, no future e serialização | [snapshot_integrity](brooks_v2_investigation_evidence/snapshot_integrity.json) | Trader/Context consumers/coordination |
| Erro de provider e classificação | [probe](brooks_v2_investigation_evidence/provider_error_reproduction.json), [client](../condor/acp/pydantic_ai_client.py) | prompt_stream/prompt/_json_object |
| Skill versus modelo | [reviews](brooks_v2_investigation_evidence/decision_reviews.json), [manual audit](brooks_v2_investigation_evidence/manual_decision_audit.json), [bounds](brooks_v2_investigation_evidence/context_boundary_audit.json) |3Skills/evidence/references |
| PM root, input e fresh analysis | [root probe](brooks_v2_investigation_evidence/pm_root_reproduction.json), [analysis probe](brooks_v2_investigation_evidence/market_analysis_reproduction.json), PM captures | Adapters/supervisor |
| Tamanhos, chamadas e modelo/backend | [input audit](brooks_v2_investigation_evidence/input_audit.json), wire JSON | Caracteres; nenhum token usage |

### Verificação realizada

Recalculei contagens e hashes nos snapshots imutáveis; auditei 120/159 intents, failed rows, GM rejections e sources. Executei reproduções offline dos readers/watcher/GM com venue falsa, helpers PM com fixture, runner de fresh analysis e agregador PromptDone. Li as três Skills/references integralmente e os prompts reais. Nenhuma consulta à venue, nova chamada LLM, mudança de containers ou novo replay foi necessária. Não executei suíte massiva: nenhuma implementação foi alterada.

Scripts foram executados fora do repo e registrados como documentação textual/resultados. Captures e wire requests copiados mantêm bytes/SHA256 originais. Metadados de análise usam paths portáteis e flags baseline; labels derivados foram corrigidos quando o delta era 100 segundos ou a origem da barra estava descrita incorretamente. Sources não selecionadas permanecem no baseline: `baseline@7760041a/<path>` significa `git show 7760041a:docs/brooks_walkforward_2026-09-20_2026-09-29/<path>`. O corte posterior é fixo, não placar em tempo real.

## 14. Open questions

1. **Lifecycle terminal:** que evidência confirmada basta em cada variante API — posição zero, executor terminal/removido, fill de fechamento — e quem assume HEDGE remanescente? A ausência de lifecycle está comprovada; a política de fechamento precisa decisão.
2. **Incidente live:** deployments com writes habilitados têm bindings MAIN flat? Exige leitura específica de estado/histórico de conta; esta investigação não consultou a venue.
3. **Pending stop versus MARKET:** qual mecanismo o produto pretende operacionalizar, e como testar correspondência entre decisão e execução? O desvio foi demonstrado, sem alteração.
4. **Origem do DSML/result-like:** modelo ou adaptação do provider? Resposta HTTP sanitizada/finish_reason e controle com mesmo packet em outro caminho distinguiriam hipóteses.
5. **Representação de sources:** schema/input com namespace inequívoco e identificação de candle reduziriam alias/coordenadas? Experimento limitado nos mesmos packets falhos, preservando validators.
6. **Structured output/protocolo:** enforcement nativo, shape narrativo menor e tool protocol consistente melhorariam compliance sem perder evidência útil? Controlar modelo/input/limite de output.
7. **Qualidade Brooks:** avaliação cega por especialista de Range/Trend/High 2/MTR/Location, principalmente R3/R4/R7/R9, pode julgar consistência? Não há gold labels no lote.
8. **Compressão macro:** raw macro versus contextos versus raw sob demanda mudam localização/oposição nos mesmos cutoffs? Há recuperação H4 stale observada, sem comparação causal.
9. **Cadência/janela:** candidatos M15 entre H1 são materialmente omitidos? Reconstruir validade no instante e medir latência antes de alterar frequência; sem candles futuros para escolher candidatos.
10. **PM fatos/history:** transmitir fatos existentes de entry/fills/barriers e comparar histórico resumido/exato muda reconciliação/HOLD? A limitação factual é certa, o efeito na action não medido.
11. **Referências:** mesmos packets ambíguos com/sem reference produzem leitura melhor ou só mais texto? Contagens atuais têm seleção por ambiguidade.
12. **Modelo:** comparar capacidade exige controlar input/protocolo e julgar ex-ante. Um único alias não permite recomendar troca com evidência.

**Conclusão:** causa do binding comprovada e exposições independentes documentadas. Nenhuma solução implementada. Este lote permanece inadequado para inferir edge; a próxima alteração deve ser escolhida a partir destas evidências antes de medir desempenho em shadow/out-forward.
