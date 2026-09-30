# Walk-forward histórico Brooks — ETH-USDT

**Status:** not_started — 0 ciclos registrados

## Método e limite de interpretação

Este é um replay retrospectivo de barras históricas. Ele resume as decisões do fluxo Brooks e os resultados da simulação que foram gravados. **Não é validação prospectiva de edge** e os resultados não demonstram desempenho futuro.

- Símbolo: `não informado`; timeframe: `H1`.
- Modelo: `não informado`; venue/conector: `não informado`.
- Janela manifestada: — até —.
- Relatório regenerado em `2026-09-30T01:59:19Z`. Reexecute `python scripts/brooks_walkforward_report.py --root /home/valdemaster/brooks-condor/condor/docs/brooks_walkforward_2026-09-20_2026-09-29` após novos registros.

## Decisões e ciclo de host

- Decisões `ENTER`: **0**; `NO_TRADE`: **0**; outras ações: **0**; sem decisão: **0**.
- Ciclos com falha registrada: **0**; aceitação do host: **0 / 0** ciclos.
- Execuções de contexto: **0**; chamadas do Position Manager: **0**.
- Resultados GM registrados: `{}`.

## Resultados de simulação

Não há trades fechados registrados. Win rate, expectancy, profit factor, MFE, MAE e R ficam sem valor; nenhum resultado foi inferido a partir da decisão do modelo.
Drawdown máximo: **N/A (sem base observável)** (N/A (sem base observável)); base: série de equity ausente.
Custos: taxas por moeda `{}` (fonte: trades; eventos: 0); funding total: **N/A (sem base observável)**.
Fills simulados: **0**; trades registrados: **0**; pontos de equity: **0**.

### Resultado por regime

Sem trades fechados com PnL e regime para agrupar.

## Ciclos horários

Ainda não há ciclos no arquivo `cycles.jsonl`.

## Dados faltantes e integridade

Fontes ausentes: `cycles.jsonl`, `role_runs/`, `simulation/trades.jsonl`, `simulation/fills.jsonl`, `simulation/equity.jsonl`.
Erros/linhas incompletas lidos:
- cycles.jsonl: arquivo ausente

Os JSONs por papel vinculados na tabela preservam o `raw_response` completo, pedido/resposta do modelo e auditoria de ferramentas. O Markdown resume o resultado para manter o relatório legível.
