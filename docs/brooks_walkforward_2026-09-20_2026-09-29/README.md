# Brooks — replay histórico de 10 dias

ETH-USDT, fronteiras de 20/09/2026 00:00 UTC a 30/09/2026 00:00 UTC (fim exclusivo).
240 decisões H1: despertares de 20/09 00:00 a 29/09 23:00 UTC, com candle de decisão fechado imediatamente antes de cada despertar.

O [relatório](REPORT.md) e [métricas](metrics.json) são atualizados durante a execução. Consulte `run_manifest.json` para distinguir lote em andamento de lote concluído.

## Evidências

- `dataset/`: candles reais e manifesto de aquisição com SHA-256; warmup fechado para as janelas de 120 barras.
- `frozen_packets/`: pacote integral de cada Trader, com D1/H4 e 120 H1/M15.
- `role_runs/`: system prompt, cada mensagem user, cada resposta e sequência de ferramentas.
- `wire_requests/`: corpo JSON exato de cada requisição HTTP enviada ao modelo, sem headers/credenciais.
- `events.jsonl`, `contexts/`, `trade_state/`: trilha completa de eventos e estado exportado.
- `cycles.jsonl`: aceitação/rejeição no host e resultado do GM por hora.
- `management_outcomes.jsonl`: decisões PM e resultados do GM.
- `simulation/`: fills, trades, equity e execução; nenhuma ordem histórica é enviada à venue.

## Método e limites

PydanticAI/OpenCode API reais, prompts e consumers atuais. Backend serializado; prioridade H1 entre eventos vencidos. Snapshots congelados; retries existentes preservados. Latência real de cada resposta avança o relógio simulado, mantendo as tools limitadas ao snapshot. PM conserva o timeout atual de 60 segundos e avalia a cada 15 minutos e nos eventos de posição.

GM e ExecutionPort atuais enviam MARKET: um trigger descrito como `pending` pelo modelo não é uma ordem stop resting. O replay conserva essa semântica e informa o estado de trigger e o tipo de execução. Fill MARKET aproximado no último close M1 conhecido, com 1 bp de slippage adverso e taxa taker de 0,04% por lado. Stops/alvos resolvidos em M1 posterior; se ambos forem atingidos, stop primeiro. OHLC da primeira fração de minuto após entrada não determina barreiras ou excursões. MFE/MAE são aproximações da resolução M1.

Saldo inicial simulado: 10.000 USDT; política GM igual à demo. Funding não modelado (zero), portanto resultados líquidos apresentados excluem funding. Sem parâmetros otimizados neste lote. Um replay retrospectivo não substitui shadow/out-forward prospectivo para validar edge.

O estado de trabalho e checkpoints ficam em `/tmp/brooks-walkforward-10d-state`; a demo anterior permanece parada com seu estado preservado.

## Validação do harness

77 testes existentes de GM, PM, contextos, confiabilidade, E2E e lifecycle passaram. Um smoke separado, com intent sintético (fora dos resultados históricos), confirmou GM real → fill MARKET simulado → snapshot PM de produção → stop em M1 → custos → restauração de checkpoint. As agendas têm exatamente 240 H1, 60 H4 e 10 D1; as leituras de bootstrap retornaram 120 barras fechadas e zero barras futuras.
