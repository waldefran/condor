# Revisão factual — interrupção do stage 1 (`at_1R`)

## Escopo e interrupção

O arquivo `at_1R` registra somente a primeira operação `ETH-USDT-1h-1789883999999` e termina em **2026-09-20 22:01 UTC**. A interrupção foi registrada como “user permitted unhedged exposure up to 5R; stage paused before implementing new ceiling”: o replay parou antes da mudança para 5R. Portanto, os números abaixo descrevem o stage legado com trava em 1R, não o novo limite de 5R.

## Position Manager

- Houve **37 tentativas** do PM: **21 decisões concluídas e aceitas**, todas `HOLD`; **16 falhas** com `TimeoutError` e `accepted: false`. Não há decisão de hedge, unwind ou fechamento emitida pelo PM entre as decisões aceitas.
- A primeira decisão aceita ocorreu às 06:15:59 UTC e a última às 15:00:00 UTC. Os dois primeiros timeouts foram iniciados às 09:30 e 10:00 UTC e registrados às 09:31 e 10:01; depois, 14 timeouts ocorreram a cada 30 minutos, iniciados entre 15:30 e 22:00 e registrados entre 15:31 e 22:01 UTC. Isso corresponde ao timeout de PM de 60 segundos. O status `rejeitado pelo host` no resumo de métricas corresponde a essas falhas; `failures.jsonl` identifica todas como timeout.

## Trava e confirmação do GM

- O MAIN LONG abriu com **0,604 ETH** a 2578,327807. No M1 fechado entre 08:51:00 e 08:51:59,999 UTC, low e close foram **2569,63**, abaixo do limite registrado de **2569,80**; `ambiguous_target_touch` foi `false`.
- O GM registrou `HEDGE`, razão-alvo `1`, quantidade `0,604`, `filled_quantity: 0.604` e `assessment: confirmed`. O fill simulado abriu a perna SHORT `wf-hedge-2` com 0,604 ETH a 2569,373037. O snapshot de hedge confirmou razão 1 e exposição líquida direcional zero.

## Resultado contabilizado na interrupção

- Nenhuma perna havia sido fechada: `realized_gross_pnl` foi **0**. O `realized_net` de **−1,2436845239104 USDT** corresponde às duas taxas de abertura: **0,6229239981712** no MAIN e **0,6207605257392** no HEDGE. O simulador também registra **0,310921080 USDT** de slippage em campo separado; funding não foi modelado e os resultados o excluem.
- No último mark registrado (**2629,75**), o MAIN tinha PnL não realizado de **+31,059004572 USDT** e o HEDGE de **−36,467685652 USDT**, combinação de **−5,408681080 USDT**. Somado ao net realizado de taxas, o arquivo de interrupção reporta **marked net de −6,6523656039104 USDT**. Esse valor é marcação com as posições ainda abertas, não PnL realizado.
- A última decisão PM concluída, às **15:00 UTC**, trazia `projected_exit_net` combinado de **−8,2143096039104 USDT**, após custos de saída modelados; `funding_mode` estava como `not_modeled`. Essa projeção é anterior aos 16 timeouts e não é uma projeção atualizada para 22:01 UTC. Não deve ser confundida com o `marked_net` da interrupção.
- Na interrupção, o estado continuava `open`: MAIN restante **0,604 ETH**, HEDGE **0,604 ETH**, sem trade fechado, sem `closed_at_ms` e sem motivo de fechamento. Não houve encerramento artificial no fim do trecho arquivado.

## Evidências arquivadas

- [Manifesto e condições da simulação](run_manifest.json), [interrupção](INTERRUPTION.json) e [métricas agregadas](metrics.json).
- [Tentativas aceitas e resultados do PM](management_outcomes.jsonl), [falhas e timeouts](failures.jsonl) e [runs individuais](role_runs/). Exemplos: [primeira decisão](role_runs/pm-pm-1789884959999-80-1-a63bb1fa.json), [primeiro timeout](role_runs/timer-pm-1789896600000-21-1-d677e464.json) e [último timeout](role_runs/timer-pm-1789941600000-46-1-1355a955.json).
- [Prova e resultado da trava do GM](long_lock_events.jsonl), [fills](simulation/fills.jsonl) e [estado hedge após a trava](trade_state/ETH-USDT-1h-1789883999999/hedge_state.json).
- [Trade simulado](simulation/trades.jsonl), [snapshot e custos](simulation/snapshot.json) e [última intenção de gestão aceita](trade_state/ETH-USDT-1h-1789883999999/latest_management_intent.json).
