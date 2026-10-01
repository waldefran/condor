# Replay DeepSeek V4.1 Flash — relatório de parada

Replay interrompido por solicitação do usuário. Fonte executada: `f1207891`;
API/Pydantic; venue simulada; prompts, estratégia e timeouts preservados.
O período completo de 240 H1 não foi concluído.

## Resultado registrado

- 131 ciclos H1: 129 concluídos e 2 falhas definitivas.
- 90 NO_TRADE, 34 ENTER_LONG, 5 ENTER_SHORT.
- Todas as 39 entradas válidas foram rejeitadas pelo GM por pending stop
  incompatível com execução MARKET-only. Zero ordens, fills e trades.
- Zero chamadas LLM de PM; nenhuma posição para gerir.
- 41 análises de contexto concluídas: 7 D1 e 34 H4, incluindo bootstrap.
- Equity simulada 10.000 USDT; PnL e custos zero.
- Último ciclo registrado: `1790330399999`, 2026-09-25T09:59:59.999000+00:00.
- Ciclo `1790333999999` estava aguardando resposta quando a parada foi solicitada.
  Após SIGTERM, foi necessário SIGKILL para interromper a chamada pendente.
  Esse ciclo interrompido não foi contado como falha do modelo nem NO_TRADE.

## Erros e recuperação

11 ciclos tiveram pedido de reparo de output; 9 concluíram depois do reparo e 2 falharam.
Os pedidos de reparo iniciais foram: 6 JSON inválido, 3 schema e 2 referências OHLC
sem correspondência. São contagens de pedidos de reparo, não de ciclos perdidos.
Nos erros de schema houve `evidence_against` como string em vez de lista,
identificador `brooks.trader-intent.v2` incorreto e `qualitative_confidence` ausente.

### Falhas definitivas

1. `ETH-USDT-1h-1789934399999`: resposta inicial com JSON inválido; após reparo,
   tentou entrar com H4 stale sem leitura raw H4 no mesmo run. Host rejeitou;
   `TRADER_DECISION_FAILED` persistido; nenhuma passagem ao GM.
2. `ETH-USDT-1h-1790251199999`: resposta inicial com JSON inválido; após ferramenta
   e reparo, `evidence_against` permaneceu string em vez de lista. Host rejeitou;
   `TRADER_DECISION_FAILED` persistido; nenhuma passagem ao GM.

Um ciclo (`ETH-USDT-1h-1790207999999`) atingiu timeout de 360s e concluiu
na segunda tentativa. Durou cerca de 8min17s. O cancelamento registrado na chamada
é `CancelledError`; a política do host recuperou o timeout. Não houve intent duplicado.

## Tempo

Média dos 131 ciclos registrados: 2min14s; mediana 1min57s.
Mínimo 39s; máximo 8min17s. Incluem tentativas/reparos, excluem análises macro anteriores.

## Limites

Não houve operação que permitisse avaliar PM, lifecycle após fechamento, hedge,
MFE/MAE ou resultado financeiro. O bloqueio de pending stop impediu toda execução.
Este lote não permite concluir edge ou qualidade financeira da estratégia.
O estado operacional e as capturas brutas foram preservados; o manifest foi marcado
como `paused`, com parada externa detalhada em `stop.json`.

`STOP_SUMMARY.json` contém os agregados verificáveis; `REPORT.md` mantém o relatório
por ciclo; `role_runs/`, `wire_requests/`, `events.jsonl` e `cycles.jsonl` preservam
as evidências. Nenhuma correção de código foi implementada nesta parada.
