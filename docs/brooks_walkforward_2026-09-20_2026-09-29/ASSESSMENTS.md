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
