# Replay interrompido a pedido — desempenho parcial

## Parada e escopo

Processo 2185589 recebeu SIGTERM, terminou a chamada corrente e saiu. O
manifest ficou `paused`, com checkpoint final preservado. Nenhuma posição foi
liquidada artificialmente para produzir um resultado fechado.

Início real: 30/09/2026 22:53:27 BRT. Checkpoint final: 30/09/2026 23:16:25 BRT,
aproximadamente 23 minutos. Relógio histórico final: 22/09/2026 02:00:36.225 UTC
(21/09 23:00:36.225 BRT). O teste não completou os 131 ciclos previstos.

Foram reproduzidos 50 ciclos: 49 decisões válidas (32 NO_TRADE, 16 ENTER_LONG,
1 ENTER_SHORT) e uma falha já existente na fonte. Os decision_time_ms vão de
1789862399999 até 1790038799999. Foram persistidos 18 contextos gravados:
4 D1 e 14 H4, incluindo bootstrap. Essas análises não foram novamente inferidas.

## Entradas e lifecycle

Dos 17 ENTER válidos, 12 candidatos ativaram: cinco aprovados pelo GM e sete
rejeitados. Outros cinco não chegaram ao GM: três tocaram a invalidação,
um foi substituído por um ENTER mais recente e um expirou.

Todos os sete bloqueios usaram a mensagem `position ownership or structure is
unresolved`. Cruzando o timestamp de cada ativação com as operações, todos
ocorreram enquanto outra MAIN estava aberta. Três durante a quarta operação
e quatro durante a quinta. Não foram bloqueios após ficar flat.

Os quatro bindings de operações encerradas ficaram `closed`, com
main_position_id limpo. O quinto permanece `reconciled`, associado à MAIN
aberta. Novas operações abriram depois dos stops anteriores. Não se observou
neste trecho a falha anterior de binding não terminal após o fechamento.

## Operações — ETH-USDT, todas LONG

| Correlation ID (sufixo decision_time_ms) | Abertura UTC | Fechamento UTC | Resultado | Líquido USDT | R líquido |
|---|---|---|---|---:|---:|
| 1789883999999 | 20/09 06:15:59 | 20/09 08:51:59 | STOP_LOSS | -6,5497 | -1,3112 |
| 1789923599999 | 20/09 17:04:59 | 20/09 21:10:59 | STOP_LOSS | -5,4522 | -1,0951 |
| 1789970399999 | 21/09 06:23:59 | 21/09 07:31:59 | STOP_LOSS | -5,6022 | -1,1222 |
| 1789988399999 | 21/09 11:04:59 | 21/09 14:54:59 | TAKE_PROFIT | +9,0213 | +1,8108 |
| 1790002799999 | 21/09 15:47:59 | aberta | MAIN 0,159 ETH | -0,6861 marcado* | — |

Todos os IDs completos têm prefixo `ETH-USDT-1h-`. *O resultado marcado da
operação aberta inclui -0,1749 de taxa de entrada e -0,5112 não realizado.
Entrada: 2749,714944; última marca: 2746,50; stop: 2718,09; TP: 2812,14.

Quatro operações fechadas: três perdas e um ganho, win rate 25%, resultado
líquido -8,5828 USDT, R médio -0,4294 e profit factor 0,5125.
Equity inicial 10000,00; equity marcada final 9990,7311 USDT: variação
-9,2689 USDT (-0,0927%), incluindo a posição aberta.
Drawdown máximo observado da série de equity: 21,1043 USDT (0,2110%).

Taxas totais: 3,0392 USDT; slippage adverso registrado: 0,7598 USDT.
O slippage já está nos preços de fill e no PnL; não deve ser subtraído outra
vez. Funding não foi modelado. São resultados parciais de um simulador com
ativação conservadora no fechamento M1, não fills de stop nativo intrabar.
Quatro operações fechadas não permitem concluir se existe edge.

## PM real e disponibilidade

54 role runs do PM via PydanticAI/API OpenCode Go, modelo
`deepseek-v4.1-flash`. 52 produziram ManagementDecisionV2 válida, todas HOLD,
e tiveram GM_MANAGEMENT_APPROVED. Nenhum CLOSE, REDUCE, MOVE_STOP ou HEDGE
foi decidido. Stop e take profit foram executados pelas barreiras existentes,
sem alteração de proteção pelo PM.

O PM avaliou periodicamente a cada 30 minutos históricos e pelos eventos
existentes. Os inputs/outputs literais e wire requests estão em role_runs/ e
wire_requests/. Ferramentas observadas: get_candles (47),
read_brooks_reference (2), get_recent_structure (1); zero erro de ferramenta
registrado.

Tempo de chamadas somado por role run, incluindo ferramentas: média 23,34s,
mediana 19,95s, mínimo 14,20s, p95 empírico 36,17s e máximo 59,96s.
Duas chamadas falharam por TimeoutError sob o timeout vigente de 60s:

- timer-pm-1789975800000-235, próximo ao stop da terceira operação;
- pm-pm-1789988699999-549, wake de abertura da quarta operação.

As falhas não derrubaram o replay; avaliações posteriores ocorreram e os
executores mantiveram a proteção. Não houve decisão fabricada para os wakes
falhos. Nenhum outro failure do PM foi registrado. A falha Trader neste trecho
é reproduzida da fonte, não uma nova inferência nesta execução.

## Evidência direta

- run_manifest.json e launch.json: identidade, backend e clocks.
- cycles.jsonl: 50 ciclos originais reproduzidos.
- entry_activation_outcomes.jsonl: 12 resultados GM de ativação.
- pending_entry_events.jsonl: estados de todos os 17 candidatos.
- management_outcomes.jsonl: 52 HOLD e aprovações do GM.
- failures.jsonl: dois timeouts do PM.
- trade_state/*/binding.json: quatro closed e um reconciled aberto.
- simulation/trades.jsonl, fills.jsonl, equity.jsonl: execução e valores.
- REPORT.md e metrics.json: consolidação derivada.
- runner.log, walkforward_checkpoint.json, venue_checkpoint.json: parada e
  estado final. O estado operacional original permanece no diretório /tmp
  registrado em launch.json; não há processo ativo desse replay.

Nenhuma ordem real foi enviada à Binance. Não se alterou estratégia, modelo,
timeout ou política do GM após observar os resultados.
