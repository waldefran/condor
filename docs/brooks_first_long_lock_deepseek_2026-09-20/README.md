# Primeira operação: LONG com trava e saída líquida não negativa

Experimento isolado sobre `ETH-USDT-1h-1789883999999`, a primeira MAIN realmente
aberta no replay anterior. O Trader e os Context Analysts são reproduzidos
das respostas gravadas do DeepSeek; somente o PM e eventuais análises pedidas
por ele fazem novas inferências via PydanticAI/API OpenCode Go.

Modelo: `custom@opencode-go:deepseek-v4.1-flash`. PM periódico a cada 30 minutos
históricos, com wakes por evento existentes. O primeiro estágio usou 60s;
o novo estágio 5R usa timeout explícito de 180s apenas neste replay, após
16 timeouts observados no estágio anterior. O default e a produção não mudam.
Janela máxima: 20/09/2026 06:00 UTC a 21/09/2026 12:00 UTC exclusivo. O runner
termina antecipadamente se esta operação fechar. Nenhuma outra entrada pode
ser criada nesta execução; não há ordem real Binance.

## Política explicitamente opt-in

- MAIN LONG: o limite inicial mantém sua identidade OHLC como referência
  estrutural. No estágio 5R ele não força trava.
  STOP_LOSS e TIME_LIMIT automáticos não encerram a compra neste modo.
- No estágio 5R, perda líquida conjunta projetada de cinco vezes o R original
  em M1 completamente fechado gera hedge de 100%
  pelo GM existente. O fill usa o fechamento conhecido e slippage adverso,
  não um preço intrabar retrospectivo. Primeiro toque tem precedência numa
  barra ambígua com TP, sem inventar a ordem dos movimentos dentro do candle.
- A leitura de confirmação acontece após avançar o relógio de execução 1ms;
  nenhum candle posterior é entregue por esse avanço. A confirmação do fill
  permanece obrigatória e os validators do GM não foram relaxados.
- PM pode reduzir/remover HEDGE quando candles M15 fechados sustentarem
  retomada Brooks e recuperação do limite original. Não há promessa de alta.
  Após confirmação do unlock, o teto de 5R é rearmado sem recalcular R.
  O PM pode justificar hedge antecipado; não precisa fazê-lo ao perder 1R.
- O resultado é conjunto: MAIN + HEDGE, realizações anteriores, taxas pagas e
  taxas/slippage estimados para as saídas restantes. Floating negativo é
  permitido. MAIN CLOSE/REDUCE é bloqueado quando a saída conjunta projetada
  é negativa ou desconhecida. Uma perna HEDGE pode realizar prejuízo ao
  destravar, mantendo a operação aberta; o encerramento final conjunto precisa
  ser não negativo. Não se confunde lucro de uma perna com lucro da operação.
- MAIN não fecha deixando HEDGE órfã. É necessário desfazer HEDGE e obter
  novo estado reconciliado antes de decidir fechar MAIN.
- TP automático só fecha uma MAIN sem HEDGE se a saída líquida projetada
  nesse preço for não negativa. TIME_LIMIT vira wake informativo; HOLD após
  vencimento estende a avaliação até o próximo intervalo PM. O horizonte
  máximo do experimento permanece finito.
- MAIN SHORT conserva stop normal; HEDGE SHORT de uma compra é uma perna da
  trava, não uma nova operação MAIN vendida.

Esta é uma política solicitada pelo usuário, não uma regra atribuída a Al
Brooks. O skill usa price action Brooks para a avaliação de retomada.
O opt-in está no packet `management_policy.applicable_risk_behavior`, incluindo
a projeção calculada pelo host. A política padrão permanece intacta.

## Limites do resultado

O bloqueio é de encerramento voluntário negativo no simulador. Não demonstra
que o mercado garantirá lucro, que a posição necessariamente recuperará ou
que não haverá liquidação/erro de execução em produção. Taxas são custos
reais da simulação desde a abertura, mesmo antes do encerramento. Funding
não é modelado; a projeção considera os custos disponíveis neste simulador.
Se não ocorrer saída válida até o horizonte, a operação permanece aberta,
com MAIN/HEDGE e floating registrados, sem encerramento artificial.

## Evidências

`run_manifest.json`, `cycles.jsonl`, `long_lock_events.jsonl`,
`long_duration_events.jsonl`, `management_outcomes.jsonl`, `events.jsonl`,
`role_runs/`, `wire_requests/`, `trade_state/` e `simulation/` registram o
fluxo. `source_artifacts/` preserva os prompts e outputs originais.
O relatório final distinguirá saída realizada, posição aberta e projeção.

Baseline: a operação original abriu 0,604 ETH LONG em 20/09 06:15:59.999 UTC
a 2578,327807 e fechou por STOP_LOSS em 08:51:59.999 UTC, líquido
−6,5497369398032 USDT. Este experimento começa da mesma entrada e altera
somente a gestão subsequente, sem consultar candles futuros na decisão.

## Estágios preservados

- `at_1R/`: teste interrompido ao receber a autorização para exposição descoberta
  até 5R. Trava confirmada, 37 tentativas PM (21 HOLD válidos, 16 timeouts),
  MAIN e HEDGE ainda abertos. Líquido marcado −6,6523656039104 USDT; não é
  encerramento realizado da operação. Evidência integral em `INTERRUPTION.json`.
- `at_5R/`: novo estado independente, mesma entrada e contextos gravados.
  1R = 4,99508 USDT original; 5R = 24,9754 USDT. O host observa o pior
  líquido projetado nos extremos de cada M1 fechado e executa ao fechamento
  conhecido. Não garante limite intrabar exato e funding não é modelado.

Os testes determinísticos provaram que a trajetória desta primeira operação
não exige a trava de 5R antes do TP original, caso o PM não antecipe hedge.
Isso é verificação do simulador, não informação entregue ao modelo no replay.

## Resultado final 5R

[Relatório completo](at_5R/FINAL_REPORT.md): 20 decisões PM HOLD válidas,
zero falhas, nenhuma trava/hedge. Fechamento pelo TP original em 20/09
15:01:59.999 UTC, líquido **+8,4280110396064 USDT (+1,687262R)**.
Pior líquido projetado observado −7,5398016994256 USDT (−1,509446R).
Binding terminal e zero posições abertas. Replay encerrado.
