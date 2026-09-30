# Avaliações do replay

## Primeira avaliação — após duas horas sem consultas periódicas

Snapshot inicial capturado em 30/09/2026 04:13 UTC (01:13 BRT). A validação posterior, em 04:18 UTC, já abrange 28 ciclos porque o lote continuou durante esta avaliação.

- 27 dos 240 ciclos H1 registrados: 21 intents aceitos e 6 ciclos falhos.
- Intents aceitos: 16 NO_TRADE, 3 ENTER_LONG, 2 ENTER_SHORT.
- GM: 1 entrada aprovada; 4 rejeitadas por `position ownership or structure is unresolved`.
- 1 trade SHORT fechado por stop: resultado líquido simulado -6,232807 USDT / -1,247784 R; MFE 1,502544 R, MAE 1,037169 R. Taxas 0,990166 USDT; slippage incorporado ao preço; funding não modelado.
- 47 sessões PM capturadas: 40 concluídas e 7 falhas (6 JSON inválido, 1 timeout).
- 11 sessões de contexto concluídas; Trader da hora seguinte em andamento no momento da avaliação.
- Falhas H1: 3 fontes de trigger/invalidation fora de M15, 1 OHLC sem correspondência exata, 1 schema inválido e 1 JSON inválido. O host barrou os intents inválidos antes do GM.
- Dois retries usaram a mesma mensagem inicial congelada; nenhuma duplicação de ciclo e nenhuma barra/contexto futuro nos 28 packets verificados em 04:18 UTC. Máximo observado: uma chamada de modelo por vez.

O fluxo real revelou um bloqueio de ownership após o fechamento do MAIN: o binding persistido permanece reconciled enquanto a posição já não aparece na venue simulada. As quatro rejeições do GM estão no trace. O lote conserva a versão fixada; esses resultados não isolam edge da estratégia porque há falhas de contrato e bloqueios operacionais.

Relatório parcial: [REPORT.md](REPORT.md). Prova dos snapshots/retries: [assessment_2h_validation.json](assessment_2h_validation.json).

## Segunda avaliação — após mais duas horas sem consultas periódicas

Snapshot inicial em 30/09/2026 06:21 UTC (03:21 BRT). A validação em 06:26 UTC já abrange 65 ciclos; o replay permaneceu em execução durante esta avaliação.

- 62 dos 240 ciclos H1 registrados no snapshot inicial, até 22/09/2026 12:59:59.999 UTC: 44 concluídos e 18 falhos.
- Intents aceitos pelo host: 30 NO_TRADE, 12 ENTER_LONG, 2 ENTER_SHORT.
- GM: 1 entrada aprovada e 13 rejeitadas pelo mesmo bloqueio de ownership após o fechamento do primeiro trade.
- Falhas H1: 9 fontes fora de M15, 5 JSON inválido, 3 schema inválido e 1 referência OHLC sem correspondência exata. Os intents inválidos não chegaram ao GM.
- Contextos: 20 sessões concluídas e 1 H4 rejeitada por schema. PM: 40 sessões concluídas e 7 falhas, sem novas sessões após o encerramento da posição.
- Resultado permanece um único SHORT fechado: -6,232807 USDT líquido simulado / -1,247784 R. Funding não modelado.
- Validação dos 65 packets: 15.600 barras H1/M15, nenhuma barra/contexto futuro, nenhum hash divergente ou ciclo duplicado. Máximo observado de chamadas simultâneas: 1.
- Três ciclos com retry conservaram a mesma mensagem inicial congelada. Prova: [assessment_4h_validation.json](assessment_4h_validation.json).

A versão medida permanece fixa. As falhas de saída do modelo e o bloqueio operacional continuam explícitos no relatório; este lote ainda não permite concluir se há edge. Nenhum ajuste de estratégia, GM, PM, hedge ou timeout foi aplicado durante a execução.

## Terceira avaliação — após mais duas horas sem consultas periódicas

Snapshot inicial em 30/09/2026 08:28 UTC (05:28 BRT). Validação no mesmo minuto: 102 packets, pois o lote continua enquanto o relatório é produzido.

- 101/240 ciclos no snapshot: 80 concluídos e 21 falhos. Intents aceitos: 60 NO_TRADE, 18 ENTER_LONG e 2 ENTER_SHORT.
- GM: 1 entrada aprovada e 19 rejeitadas; permanece o bloqueio de ownership já documentado. Um único trade encerrado, -6,232807 USDT / -1,247784 R.
- Falhas H1 acumuladas: 10 fontes fora de M15, 6 JSON inválido, 3 schema inválido e 2 referências OHLC sem correspondência.
- Contextos: 32 sessões concluídas, 2 falhas. PM permanece em 40 sessões concluídas e 7 falhas.
- 24.480 barras validadas em 102 packets; nenhum dado futuro, hash divergente ou ciclo duplicado. Concorrência máxima observada: 1. Os três retries registrados preservam a mensagem inicial.
- Prova: [assessment_6h_validation.json](assessment_6h_validation.json). A versão e a estratégia medidas continuam sem alterações.
