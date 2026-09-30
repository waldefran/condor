# Brooks V2 replay — space-bunny-free

Nova execução, solicitada em 30/09/2026, com as correções de lifecycle, contrato,
runtime e fidelidade do commit `03b4aed988b6dc6bae48a2ed479d92361f5d7563`.

- Modelo de todos os roles: `custom@opencode-go:space-bunny-free`.
- Transporte: PydanticAI, API `https://opencode.ai/zen/go/v1/chat/completions`.
- Preflight real concluído: a API respondeu `{"ok":true}` usando PydanticAI.
- O modelo também foi confirmado no [catálogo oficial da API](https://opencode.ai/zen/go/v1/models).
- ETH-USDT; mesmo dataset e agendamento do replay anterior: 20/09/2026 00:00 UTC
  até 30/09/2026 00:00 UTC exclusivo, 240 despertares H1.
- Estado de conta/posições/bindings zerado. Equidade simulada de 10.000 USDT,
  taxa taker 0,04% e slippage adverso de 1 bp; funding não modelado.
- D1/H4/Trader/PM/GM usam os consumers de produção. A venue é simulada;
  nenhuma ordem é enviada à Binance ou à conta demo real.
- Código/prompts/references executados de um snapshot Git fixo. O SHA e os hashes
  dos scripts ficam em `run_manifest.json`; dados históricos conservam seu manifest.

## Resultados em geração

`REPORT.md` e `metrics.json` são atualizados a cada checkpoint. A evidência completa
fica em `cycles.jsonl`, `events.jsonl`, `role_runs/`, `wire_requests/`,
`frozen_packets/`, `contexts/`, `trade_state/` e `simulation/`.
Cada role run preserva SYSTEM, mensagens user, respostas e chamadas reais de API.

Não misturar este lote com o replay DeepSeek anterior. Falha de role não é
NO_TRADE; pending stop rejeitado pelo GM não é uma entrada executada. Pending stop
nativo permanece indisponível; esta execução não simula fill artificial nesse caso.

## Comando

```sh
python scripts/brooks_walkforward.py \
  --dataset /tmp/brooks-walkforward-10d-data \
  --state /tmp/brooks-walkforward-space-bunny-free-20260930-state \
  --output /home/valdemaster/brooks-condor/condor/docs/brooks_walkforward_space_bunny_free_2026-09-20_2026-09-29 \
  --agent-key custom@opencode-go:space-bunny-free \
  --start 2026-09-20T00:00:00Z --end 2026-09-30T00:00:00Z \
  --git-head <SHA registrado no manifest>
```

O processo usa o Python da `.venv` do projeto. O snapshot fica em `/tmp`; o output
acima fica nesta pasta do repositório. O checkpoint operacional e o log ficam em
`/tmp/brooks-walkforward-space-bunny-free-20260930-state`, separados do lote antigo.
