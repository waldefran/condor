# Brooks V2 — replay corrigido com DeepSeek V4.1 Flash

Nova execução solicitada em 30/09/2026, após interrupção e remoção dos dados do
replay space-bunny-free. O lote DeepSeek antigo permanece separado.

- Todos os roles: `custom@opencode-go:deepseek-v4.1-flash` via PydanticAI/API.
- Endpoint: `https://opencode.ai/zen/go/v1/chat/completions`.
- ETH-USDT; mesmo dataset histórico de 20/09/2026 00:00 UTC até
  30/09/2026 00:00 UTC exclusivo; 240 despertares H1 previstos.
- Estado novo, sem reutilizar decisões, contextos, bindings ou posições anteriores.
- Código, prompts e referências executados de snapshot Git fixo, registrado no manifest.
- Venue simulada; nenhuma ordem real. Equidade inicial 10.000 USDT,
  taker fee 0,04%, slippage adverso 1 bp; funding não modelado.
- Mantidas as correções de lifecycle/fidelidade e a estratégia, prompts e timeouts.
- Pending stop nativo permanece indisponível: rejeitado pelo GM antes de qualquer write;
  não se converte em MARKET nem se fabrica fill.

`REPORT.md` e `metrics.json` são atualizados nos checkpoints. `role_runs/` e
`wire_requests/` preservam as mensagens e respostas reais; `cycles.jsonl`,
`events.jsonl`, `frozen_packets/`, `contexts/`, `trade_state/` e `simulation/`
registram o fluxo. O comando, PID, paths e preflight ficam em `launch.json`.

Esta execução é replay retrospectivo; não demonstra edge prospectivo.
