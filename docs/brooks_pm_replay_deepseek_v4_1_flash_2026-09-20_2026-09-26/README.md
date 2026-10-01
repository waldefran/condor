# Replay das decisões gravadas com PM real

## Escopo

Reutiliza os 131 ciclos e os 41 contextos aceitos de
`../brooks_walkforward_deepseek_v4_1_flash_corrected_2026-09-20_2026-09-29/`.
Trader, D1 e H4 não são novamente inferidos. Os dois ciclos originalmente
falhos continuam falhos; não são substituídos por decisões fabricadas.

O PM usa `custom@opencode-go:deepseek-v4.1-flash`, por PydanticAI e API
OpenCode Go. GM, reconciliation e ExecutionPort usam o fluxo existente,
ligado exclusivamente à venue histórica simulada. Não há ordem Binance real.
A periodicidade do PM é 1.800 segundos, além dos wakes por evento existentes.
O timeout do PM permanece o vigente, registrado no manifest.

Janela: 2026-09-20 00:00 UTC até 2026-09-26 13:00 UTC exclusivo. As decisões
originais terminam em 2026-09-25 09:59:59.999 UTC; o restante é uma cauda de
gestão, sem novas decisões Trader, para observar expiração e TIME_LIMIT.

## Ativação de pending stop no simulador

- Cada resposta fica disponível somente no instante de conclusão do replay
  original. Isso também ocorre durante a espera de uma chamada PM.
- Somente M1 completamente iniciado após essa disponibilidade é elegível.
- LONG exige máxima estritamente acima do trigger e fechamento acima dele;
  SHORT exige mínima estritamente abaixo e fechamento abaixo.
- Toque na invalidação cancela; quando trigger e invalidação aparecem no mesmo
  M1, o cancelamento prevalece.
- O fill é MARKET no fechamento M1 observado, com slippage adverso e as
  validações existentes do GM. Não é um fill retroativo no preço do trigger
  nem uma simulação exata de um stop nativo intrabar.
- Expiração segue o limite existente de duas horas desde decision_time_ms.
  Novo ENTER do mesmo símbolo cancela o candidato anterior. NO_TRADE não
  cancela um candidato anterior; invalidação e expiração continuam valendo.
- A resposta original fica intacta. Um intent de execução derivado altera
  somente trigger_status para triggered, com prova OHLC registrada antes da
  submissão. O PM recebe o original e os fills/posição reais da simulação.
- Uma transição terminal não submete novamente o mesmo candidato. O replay
  mantém checkpoints para retomada entre operações; não demonstra atomicidade
  contra crash de processo exatamente durante uma submissão.
- O scheduler entrega POSITION_OPENED no minuto observado antes de avançar
  ao próximo timer. A posição pode fechar enquanto o PM aguarda a API; o GM
  continua validando o estado de execução antes de aceitar qualquer ação.

## Evidências

- `run_manifest.json`: modelo, parâmetros, hashes, clocks e estado da execução.
- `source_artifacts/`: cópia dos prompts, packets, respostas e wire requests
  originais, preservando o backend e o timing de cada análise.
- `cycles.jsonl`: decisões reproduzidas e apontadores para evidências originais.
- `pending_entry_events.jsonl`: pending, canceled, expired e triggered.
- `entry_activation_outcomes.jsonl`: prova de ativação e resposta do GM.
- `role_runs/` e `wire_requests/`: chamadas reais do PM e análises read-only
  solicitadas por ele, quando houver.
- `management_outcomes.jsonl`: decisão do PM e resultado determinístico do GM.
- `events.jsonl`, `trade_state/` e `simulation/`: lifecycle, fills e posições.
- `report.md` e `metrics.json`: relatório derivado, regenerado por checkpoint.

Avaliações de andamento serão feitas em intervalos de uma hora de relógio
real. Este experimento verifica o fluxo de gestão; seus resultados não
demonstram edge e não são diretamente comparáveis ao replay que rejeitava
todas as entradas pendentes.

## Validação antes de iniciar

Testes de pending stop, integração GM/reconciliation/PM, replay gravado,
fidelidade do walk-forward, fidelidade do PM e PM: 35 testes aprovados.
Nenhum módulo de produção GM, PM, hedge, contrato ou ExecutionPort foi
alterado nesta implementação; as mudanças de ativação são restritas ao harness.
