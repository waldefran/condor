# PM: proteção de lucro e análises recentes

Data: 2026-10-01. Branch: `feat/brooks-agents-v1`.

## Problema observado

O replay pausado de `ETH-USDT-1h-1790002799999` registrou 203 decisões
`HOLD`, sem hedge. O limite de 5R não foi atingido. Isso não provava uma
restrição do GM: hedge discricionário antes de 5R já era permitido.

Havia limitações concretas na informação entregue ao PM:

- O snapshot expunha um contexto D1 adaptado para V1, sem os campos completos
  de `MarketContextV2` e sem o H4 completo.
- O replay de uma única operação descartava os ciclos posteriores do Trader,
  deixando a análise da entrada como sua única análise disponível.
- A Skill não colocava proteção de lucro como missão principal e vinculava
  o desbloqueio ao retorno acima do limite estrutural original.

O comportamento anterior permanece documentado em
`brooks_pm_selected_1790002799999_5R/`; suas capturas não foram substituídas.

## Nova instrução de gestão

A Skill usada no system prompt do PM e sua referência de gestão agora orientam:

1. Buscar maximizar o lucro líquido da operação existente, preservar exposição
   favorável, proteger ganhos abertos e conter deterioração estrutural.
2. Comparar `HOLD`, hedge parcial/total, aumento e desbloqueio do hedge,
   além das reduções/fechamentos que a política permitir.
3. Usar evidência recente de OHLC M15 fechado em cada decisão discricionária,
   inclusive `HOLD`; consultar as barras pelas tools quando necessário.
4. Permitir hedge em qualquer avaliação do PM, inclusive com lucro, perto de
   zero ou com prejuízo flutuante antes de 5R. `SAFE` e perda inferior a 5R
   não bastam como justificativa para continuar descoberto.
5. Contabilizar custos no resultado líquido; custos isoladamente não vetam nem
   desestimulam um hedge sustentado pela estrutura.
6. Desbloquear com evidência M15 atual de retomada e condição concreta de
   falha, sem exigir recuperar o preço do limite estrutural da entrada.
7. Dar justificativa concisa e evidência contrária; não repetir `HOLD` apenas
   porque as decisões anteriores foram `HOLD`.

O host explicita esses objetivos em `management_policy.applicable_risk_behavior`:

```json
{
  "management_objective": "maximize_operation_net_profit",
  "discretionary_hedge_timing": "any_management_wake",
  "hedge_cost_policy": "account_in_net_never_standalone_veto",
  "hedge_objectives": ["protect_open_profit", "limit_structural_deterioration"]
}
```

No experimento LONG selecionado, acrescenta
`unlock_policy: "fresh_closed_m15_recovery_structure"` e identifica a política
como `experimental-2`.

## O que chega ao PM

O primeiro pacote congelado contém `macro_contexts.D1` e `macro_contexts.H4`
com o `MarketContextV2` completo e `freshness` (`current`, `stale` ou `missing`).
O campo `latest_trader_intent_freshness` identifica a atualidade da análise
mais recente do Trader. A intenção original continua separada.

A atualidade é calculada contra o último fechamento na grade absoluta de epoch
para D1, H4 e H1. Dados futuros, de outro símbolo ou de timeframe incompatível
não são apresentados como contexto utilizável. Contexto indisponível não
invalida sozinho um snapshot operacional válido. As leituras pelas tools usam
o mesmo pacote congelado daquela avaliação.

`get_market_context()` entrega o bundle D1/H4 quando presente; snapshots antigos
mantêm o fallback do contexto legado. Contextos são interpretações falíveis.
OHLC fechado é a referência da estrutura atual, sem votação direcional.
Um `NO_TRADE` recente não manda fechar a posição, e um `ENTER_*` contrário não
autoriza abrir outra.

No replay de uma operação, análises posteriores aceitas do Trader passam a
atualizar apenas seu contexto, no instante original de conclusão registrado
na fonte. Falhas não substituem a última análise aceita. Essas atualizações são
auditadas em `trader_context_updates.jsonl` e não produzem eventos de entrada,
novos candidatos, ciclos de execução ou ordens. D1/H4 continuam disponíveis no
instante de conclusão registrado. O próximo wake do PM captura os resultados
que já estavam disponíveis; uma chamada em andamento conserva seu snapshot.

## Limites preservados

- GM, ExecutionPort, ledger, ownership, reconciliação e limites de margem/mode
  não foram modificados.
- O LLM continua sem write tools e sem calcular quantity.
- O guard determinístico de 5R e o R inicial congelado continuam ativos como
  proteção final. A avaliação do PM continua com sua cadência existente.
- O piso líquido não negativo para redução/fechamento de MAIN LONG no
  experimento continua considerando MAIN + HEDGE e custos.
- O TP permanece ativo e fixo. Esta alteração não implementa movimentação
  de TP nem criação/movimentação de stop.
- Funding não modelado continua explicitamente identificado no replay.

## Validação

Comando executado:

```sh
.venv/bin/pytest -q \
  tests/test_brooks_pm_fidelity.py \
  tests/test_brooks_pm.py \
  tests/test_brooks_pm_runner.py \
  tests/test_brooks_prompt_runtime.py \
  tests/test_brooks_first_long_replay.py \
  tests/test_brooks_pm_profit_policy.py \
  tests/test_brooks_long_lock_venue.py \
  tests/test_brooks_long_lock_policy.py \
  tests/test_brooks_market_tools.py \
  tests/test_brooks_contracts.py
```

Resultado: **85 passed**, 12 warnings existentes sobre o campo `schema` do
Pydantic, em 12,17s. Nenhum teste foi pulado neste ambiente.

Os testes novos verificam entrega dos contextos completos no primeiro pacote
do PM e na tool, congelamento após alteração dos arquivos, atualidade e rejeição
de contexto futuro/de outro símbolo. Um teste do replay verifica a atualização
posterior do Trader sem duplicar a operação.

Dois casos percorrem PM e GM reais com resposta de modelo injetada, venue
simulada e candles históricos: hedge total aprovado com lucro aberto e hedge
total aprovado com perda abaixo de 5R. Ambos mantêm uma única MAIN, geram apenas
o fill do hedge e não acionam o guard obrigatório. Esses testes demonstram
admissibilidade e fidelidade do fluxo; não medem a escolha do novo prompt por
um LLM real nem melhora de resultado financeiro.

O replay anterior permanece pausado. Não foi reiniciado nem houve chamada
nova de inferência externa nesta validação. Uma futura comparação deve usar
estado e capturas novos, preservando os artefatos anteriores.
