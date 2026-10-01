# Walk-forward histórico Brooks — ETH-USDT

**Status:** completed — 1 ciclos registrados de 1 esperados; 0 restantes

## Método e limite de interpretação

Este é um replay retrospectivo de barras históricas. Ele resume as decisões do fluxo Brooks e os resultados da simulação que foram gravados. **Não é validação prospectiva de edge** e os resultados não demonstram desempenho futuro.

- Símbolo: `ETH-USDT`; timeframe: `H1`.
- Modelo: `custom@opencode-go:deepseek-v4.1-flash`; venue/conector: `não informado`.
- Janela manifestada: 2026-09-20 03:00:00 -03 até 2026-09-21 09:00:00 -03.
- Relatório regenerado em `2026-10-01T13:31:04Z`. Reexecute `python scripts/brooks_walkforward_report.py --root /home/valdemaster/brooks-condor/condor/docs/brooks_first_long_lock_deepseek_2026-09-20/at_5R` após novos registros.

## Decisões e ciclo de host

- Decisões `ENTER`: **1**; `NO_TRADE`: **0**; outras ações: **0**; sem decisão: **0**.
- Ciclos `failed`: **0**; aceitação do host: **1 / 1** ciclos.
- Propostas sem intent aceito: ENTER **1**, NO_TRADE **0**; tipos de falha: `{}`.
- Execuções de contexto (inclui bootstrap): **0**; Position Manager: **20**; status por papel: `{"POSITION_MANAGER": {"completed": 20}}`.
- Resultados GM registrados: `{"GM_ENTRY_APPROVED": 1}`.

### Contextos macro de bootstrap

Nenhuma chamada de contexto marcada como bootstrap foi capturada.

## Resultados de simulação

Trades fechados: **1** (1 com PnL disponível); win rate: **100,00%**; expectancy líquida por trade: **8,4280**; profit factor: **N/A (sem base observável)**.
PnL líquido total: **8,4280**; MFE médio (R): **1,9797**; MAE médio (R): **1,2295**; R realizado médio: **1,6873**.
Drawdown máximo: **10,1351** (0,10%); base: série de equity ordenada por timestamp.
Custos: taxas por moeda `{"unidade não informada": 1.2497190883936}` (fonte: fills; eventos: 2); funding total: **0,0000** (assumption explícita do manifest: not modeled (zero); net results exclude funding).
Fills simulados: **2**; trades registrados: **1**; pontos de equity: **552**.

### Resultado por regime

| Regime | Trades fechados com PnL | Win rate | Expectancy líquida | Profit factor | R médio |
|---|---:|---:|---:|---:|---:|
| H4 bull-trend (channel) with an unconfirmed reversal candidate; M15/H1 in a tight 12-bar range at the low of a strong bear breakout | 1 | 100,00% | 8,4280 | N/A (sem base observável) | 1,6873 |

## Ciclos horários

| Horário BRT | Round | Ciclo / decisão | Host | GM | Inputs congelados e contexto macro | Papéis, tools e JSON integral | PM, atribuição e lifecycle | Fills |
|---|---|---|---|---|---|---|---|---:|
| 2026-09-20 02:59:59 -03 | `recorded-1h-1789883999999` | completed / ENTER<br>packet `dbeeb62fb19d…` | completed | GM_ENTRY_APPROVED — binding registrado | H1: [source_artifacts/frozen_packets/7b7beb583e34fb03d41b2116b91aa23e36201131b557b5e5b6dd1fcba381180d.json (raw.H1)](source_artifacts/frozen_packets/7b7beb583e34fb03d41b2116b91aa23e36201131b557b5e5b6dd1fcba381180d.json) — sha256:3019c0b7b715… (derivado); 120 barras<br>M15: [source_artifacts/frozen_packets/7b7beb583e34fb03d41b2116b91aa23e36201131b557b5e5b6dd1fcba381180d.json (raw.M15)](source_artifacts/frozen_packets/7b7beb583e34fb03d41b2116b91aa23e36201131b557b5e5b6dd1fcba381180d.json) — sha256:9c4427ccc7de… (derivado); 120 barras<br>D1 freshness=current; bull-trend / range; lag 6,00 h; [source_artifacts/frozen_packets/7b7beb583e34fb03d41b2116b91aa23e36201131b557b5e5b6dd1fcba381180d.json (macro_contexts.D1)](source_artifacts/frozen_packets/7b7beb583e34fb03d41b2116b91aa23e36201131b557b5e5b6dd1fcba381180d.json) — sha256:263a57d4aac5… (derivado)<br>H4 freshness=current; bull-trend / channel; lag 2,00 h; [source_artifacts/frozen_packets/7b7beb583e34fb03d41b2116b91aa23e36201131b557b5e5b6dd1fcba381180d.json (macro_contexts.H4)](source_artifacts/frozen_packets/7b7beb583e34fb03d41b2116b91aa23e36201131b557b5e5b6dd1fcba381180d.json) — sha256:82d476de3d99… (derivado) | **TRADER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `ENTER`) — ferramentas: sem chamadas registradas; [JSON integral](source_artifacts/role_runs/clock-1h-1789883999999-10-1-e83eae42.json); sha256 `a7c65bcb5154…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/pm-pm-1789884959999-80-1-7eaa591c.json); sha256 `92ccb71082f1…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/pm-pm-1789884959999-81-1-ff1ee3df.json); sha256 `8b1f3879ce91…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789885800000-15-1-1b40243d.json); sha256 `2d5c34bab78b…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789887600000-16-1-79b5e2ad.json); sha256 `411001da735c…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789889400000-17-1-caa67ceb.json); sha256 `bca7997b6fa0…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789891200000-18-1-9a8ae2c4.json); sha256 `b23236a39d93…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789893000000-19-1-8e840c47.json); sha256 `151ce8b4765c…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789894800000-20-1-cdca2d83.json); sha256 `5d5e2a065562…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789896600000-21-1-facf5b64.json); sha256 `d2148c8b097c…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789898400000-22-1-7c17b237.json); sha256 `44870f58dbb7…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789900200000-23-1-11f76f4e.json); sha256 `beb7ad602056…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789902000000-24-1-a1e62505.json); sha256 `dda891587008…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789903800000-25-1-c186080d.json); sha256 `c53b09352449…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789905600000-26-1-b6288bc1.json); sha256 `ed3717dc1101…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789907400000-27-1-afb06139.json); sha256 `afc19120f28e…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789909200000-28-1-bf123931.json); sha256 `f142cf1fd9f3…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789911000000-29-1-8d20d57f.json); sha256 `6c5214d0c85c…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789912800000-30-1-255be6da.json); sha256 `19ebc8fcb233…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789914600000-31-1-e97cc175.json); sha256 `c3e5c6ff073a…`<br>**POSITION_MANAGER** (custom@opencode-go:deepseek-v4.1-flash, completed; saída `NO_TRADE`) — ferramentas: 1. get_candles: completed; [JSON integral](role_runs/timer-pm-1789916400000-32-1-caf0b90b.json); sha256 `3c7d635abf53…` | completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>completed: HOLD<br>trade ETH-USDT-1h-1789883999999: closed, encerrado 2026-09-20 12:01:59 -03, motivo TAKE_PROFIT, PnL líquido 8,4280 (reportado), ações PM: OPEN_MAIN → TAKE_PROFIT | 2: BUY 0.604 @ 2578.327807 (MAIN_OPEN) em 2026-09-20 03:15:59 -03; fee 0.6229239981712<br>SELL 0.604 @ 2594.350538999999999999999999 (TAKE_PROFIT) em 2026-09-20 12:01:59 -03; fee 0.6267950902223999999999999996; atribuição "TAKE_PROFIT" |

## Dados faltantes e integridade

Nenhuma fonte ausente, erro de leitura ou divergência entre hash informado e arquivo foi encontrada.

Os JSONs por papel vinculados na tabela preservam o `raw_response` completo, pedido/resposta do modelo e auditoria de ferramentas. O Markdown resume o resultado para manter o relatório legível.
