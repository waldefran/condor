# Brooks — prompts fundamentados em Al Brooks

## Escopo e fontes

Validação via **PydanticAI → API OpenCode**, modelo `custom@opencode-go:deepseek-v4.1-flash`.
Execução final iniciada em 2026-09-30T00:35:03.511000+00:00; encerrada em 2026-09-30T00:40:50.136000+00:00.
As chamadas usaram as barras históricas congeladas do trace de 28/09/2026. O PM recebeu
uma posição LONG protegida **sintética**, identificada como fixture; não era uma posição
observada na venue. Todas as avaliações deste relatório tiveram zero writes de execução.

Foram conferidos índices e trechos relevantes dos quatro livros fornecidos em Downloads:

- *Reading Price Charts Bar by Bar*: contexto de sinais e stops pendentes; PDF pp. 20, 28–29, 41–44, 324–325.
- *Trading Price Action Trends*: contagem High/Low, Always-In, espectro tendência/range, canais e spike/channel; páginas detalhadas nas notas das Skills.
- *Trading Price Action Trading Ranges*: pullbacks/ranges, Trader's Equation, premissa e gestão; caps. 11, 21–22, 24–25, 27 e 29.
- *Trading Price Action Reversals*: reversão maior versus menor, follow-through e casos específicos de volume diário; caps. 2–3, 9–10.

Os números acima são páginas PDF de base 1. Os resumos são próprios; os livros integrais
não foram copiados para o repositório. Veja as notas de
[contexto](../agents/brooks_price_action/skills/brooks-market-context/references/source-notes.md),
[entrada](../agents/brooks_price_action/skills/brooks-trade-entry/references/source-notes.md) e
[gestão](../agents/brooks_price_action/skills/brooks-position-management/references/management-evidence.md).

A conferência incluiu fontes oficiais: Brooks define setup em relação ao sinal e ao
contexto à esquerda no [manual de candlesticks](https://www.brookstradingcourse.com/how-to-trade-manual/candlestick-charts/)
e distingue sinal, entrada e stop pendente no [manual de stop orders](https://www.brookstradingcourse.com/how-to-trade-manual/stop-orders/).
Sua [configuração intraday](https://www.brookstradingcourse.com/how-to-trade-manual/day-trading-setup/)
inclui EMA; a ausência de indicadores no pacote Condor é uma escolha deste projeto.

## Alterações

- A identidade Al Brooks e o procedimento bar-by-bar aparecem explicitamente no system de cada role.
- Procedimento: fatos observados → contexto/regime/localização → setup e melhor caso contrário → trigger/invalidation ou motivo de abstinência → evidência concisa.
- Ausência de volume, volume profile, DOM, order flow, footprint, notícias ou indicadores, por si só, não é dado obrigatório faltante. Volume fornecido continua opcional; casos específicos de volume nos livros não foram negados.
- Candles futuros não são informação faltante. Contextos descrevem condições observáveis que mudariam a classificação.
- High 1/High 2 e Low 1/Low 2 são escritos por extenso para evitar confusão com timeframe H1.
- As Skills de contexto e gestão foram alinhadas aos contratos de produção V2. O PM recebe apenas as ações suportadas e mantém os arrays de execução vazios; o GM compila as ações.
- Foram documentadas as assinaturas reais das tools e o protocolo JSON exclusivo. Não foram adicionadas tools ou permissões.
- Validadores standalone de contexto/PM reutilizam os modelos do host; o de entrada preserva V1 e verifica as restrições M15 de V2 e o tipo descritivo de setup.

D1/H4, freshness, gatilho H1, fontes M15 obrigatórias para entradas, raw H4 obrigatório
quando stale/missing, snapshot congelado, retry, coordenação de backend, GM, hedge,
ExecutionPort e timers mantiveram seus códigos e configurações. O adapter de candles
e os leitores market/PM passaram a vincular consultas ao decision time, conforme
a correção de restart descrita abaixo.

## Falhas encontradas e correções

1. Na preparação do harness, barras copiadas do pacote interno não tinham o marcador
   externo `closed=true`. O ClosedBarGate rejeitou-as antes de qualquer chamada contextual
   ou Trader. O adapter do harness passou a declarar o fechamento após verificar timestamps;
   preços e tempos não foram alterados. Esse erro era da fixture, não de um candle da venue.
2. A primeira chamada real do PM retornou `HOLD`, mas `evidence_against` era uma string.
   O host rejeitou a resposta sem executar nada. O prompt V2 agora explicita arrays de
   strings não vazios, inclusive para um único argumento, e elimina instruções V1 concorrentes.
3. O primeiro D1 real tentou uma tool com argumentos inexistentes e adicionou markup DSML
   após o JSON. O host rejeitou o retorno. As assinaturas e a proibição desse markup foram
   explicitadas. A repetição manual após essa revisão de prompt concluiu com JSON válido.
   Não foi um retry automático de erro transitório e não houve mudança na política de retries.

O trace guarda essas respostas e falhas; nenhuma resposta rejeitada foi convertida
artificialmente em decisão. O gate continua rejeitando saídas inválidas.

## Contextos validados

| Role | Decision time UTC | Classificação | missing_information |
|---|---|---|---|
| H4 | 2026-09-28T11:59:59.999000+00:00 | trading-range / range | `[]` |
| D1 | 2026-09-27T23:59:59.999000+00:00 | bull-trend / breakout-spike | `[]` |

Antes, o D1 mencionava volume profile/order flow ausentes e o H4 mencionava delta,
volume-at-price e ausência de barras futuras. Na validação nova, ambos retornaram `[]`.
O D1 classificou bull-trend e o H4 trading-range: essa divergência permaneceu evidência
estrutural, sem votação ou recomendação dos analistas.

## Trader: ponta a ponta

| Avaliação | Decision time UTC | Decisão | Motivo | Attempt | Intent events |
|---|---|---|---|---|---|
| Validação inicial | 2026-09-28T12:59:59.999000+00:00 | NO_TRADE | poor_location | 1 | 1 |
| Validação inicial | 2026-09-28T13:59:59.999000+00:00 | NO_TRADE | poor_location | 1 | 1 |
| Após correção D1 | 2026-09-28T12:59:59.999000+00:00 | NO_TRADE | poor_location | 1 | 1 |

São **dois fechamentos H1 distintos**, com o primeiro repetido numa store isolada
após a correção do D1. Na validação inicial o D1 estava explicitamente missing;
na última avaliação D1 e H4 estavam `current`. Em cada caso: evento H1 → packet
persistido/congelado → role real → validação host → um intent persistido → um evento
TRADER_INTENT_CREATED → GM shadow → zero execução. As 120 barras H1 e as 120 M15
foram comparadas exatamente ao trace original; nenhum candle futuro entrou.

A captura final lista exatamente `get_closed_candles`, `get_recent_structure`,
`get_volatility`, `read_brooks_reference`. O schema TradeIntentV2 completo está na
primeira mensagem. As saídas e todas as mensagens de tool estão no trace.

## PM: posição protegida sintética

A posição fixture tinha um MAIN LONG, stop reduce-only cobrindo a quantidade,
margem SAFE e política exigindo stop. O PM final escolheu `HOLD`,
com `1` decisão persistida, `1` evento shadow
publicado e zero execução. A tese foi avaliada contra premissa/invalidação e proteção;
PnL negativo isolado não foi transformado em quebra estrutural. Nenhuma posição real
foi criada para esta avaliação.

## Capturas e tamanho

- [System do Trader](brooks_trader_prompt.txt): texto recebido pelo serializer API, sem cabeçalho adicional.
- [System do PM](brooks_pm_prompt.txt): mesmo critério, sem cabeçalho adicional.
- [Request real do Trader](brooks_trader_llm_request.json): corpo JSON interceptado no envio HTTP real; contém messages, schema completo e packet.
- [Trace desta validação](brooks_al_brooks_prompt_validation_trace.json): systems do builder, mensagens enviadas, respostas brutas, tempos, resultados e probes rejeitados.

PydanticAI remove o newline final do system do Trader ao serializar. O arquivo de
request e o TXT foram comparados ao corpo enviado; a primeira mensagem user também
foi comparada literalmente. Headers/chaves não estão nas capturas.
Referências carregadas por tool nesta amostra: `["position_management.management_evidence"]`.
Os livros e referências completos não são carregados por padrão.

Tamanho em caracteres, comparado ao HEAD anterior `b0353f4b`:

| Artefato | Antes | Depois |
|---|---:|---:|
| `docs/brooks_trader_prompt.txt` | 5226 | 7041 |
| `docs/brooks_pm_prompt.txt` | 11963 | 16277 |
| `agents/brooks_price_action/skills/brooks-trade-entry/RUNTIME.md` | 4821 | 6368 |
| `agents/brooks_price_action/skills/brooks-market-context/RUNTIME.md` | 3423 | 5786 |

O conteúdo operacional cresceu para tornar método, argumentos e limites inequívocos;
a documentação e os livros continuam fora do pacote inicial do Trader.

## Restart live e corte temporal de candles

Ao retomar o demo às 00:43 UTC, o H1 pendente tinha decision time 00:00 UTC.
A fonte buscava candles usando a hora atual: duas barras M15 fechadas após o
corte ocuparam parte da janela de 121. O gate removeu-as, deixando 119 barras,
e persistiu `TRADER_DECISION_FAILED` / `ClosedBarError` antes da chamada LLM.
Não houve intent ou ordem nessa rodada. O registro failed foi preservado.

Foi adicionado `HummingbotCandleSource.at_decision_time()`, com relógio limitado
por `min(now, decision_time)`. Os leitores de mercado e PM vinculam essa fonte ao
seu snapshot; fontes genéricas continuam usando o protocolo de três argumentos.
Não houve mudança em GM, hedge, ExecutionPort, prioridade ou política de retry.

A regressão reproduz restart 43 minutos após H1. A consulta à API Hummingbot real,
feita às 00:51 UTC para o mesmo corte de 00:00 UTC, confirmou:

| Janela | Barras | Último close_time_ms | Barras futuras |
|---|---:|---:|---:|
| H1 | 120 | 1790726399999 | 0 |
| M15 | 120 | 1790726399999 | 0 |

Esse probe foi somente leitura de mercado, sem nova chamada LLM ou ordem. O demo
foi retomado com o estado anterior preservado, conector `binance_perpetual_demo`,
PydanticAI/API OpenCode e `shadow_mode: false`, como na configuração já autorizada.
O ciclo failed anterior não foi reescrito; o próximo H1 segue a grade do clock.
Nenhum container foi alterado.

## Verificação

- Suíte Brooks completa + Condor selecionado: **565 passed**, 12 warnings preexistentes de `schema`, 14.70 s.
- `tests/test_brooks_prompt_runtime.py`: **7 passed**.
- Frontmatter das três Skills: válido.
- Exemplos/validadores: MarketContextV2 válido; wrapper legado e trade_bias rejeitados; entrada real M15 válida e fonte H1 rejeitada; PM V2 válido e scalar evidence, PROTECT e order request rejeitados.
- Referências dentro do limite de 12.000 caracteres e `git diff --check` limpo.
- Nenhum arquivo do GM/hedge/ExecutionPort/clock/retry foi modificado. Em PM, apenas a fonte de candles read-only foi vinculada ao snapshot; ações e fluxo de gestão permaneceram iguais.
- Todas as respostas finais reais também passaram nos validadores standalone. Percentuais factuais de retração não são tratados como probabilidade; odds/chance numéricos continuam rejeitados.

Esta amostra valida transmissão, contrato e fluxo shadow; não mede edge. O relatório
histórico de dez ciclos e seu trace originais foram preservados. O passo seguinte
continua sendo shadow/out-forward, com custos e resultados observados.
