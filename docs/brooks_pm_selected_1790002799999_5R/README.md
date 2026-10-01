# Replay isolado do PM — ETH-USDT-1h-1790002799999

Autorização: usuário pediu testar esta decisão, mantendo a política LONG
de líquido conjunto não negativo e exposição descoberta até 5R originais.

## Método congelado antes de iniciar

- Janela: **21/09/2026 15:00 UTC a 24/09/2026 15:00 UTC**, fim exclusivo,
  72 horas de relógio histórico. MAIN esperada após confirmação às 15:47:59.999
  do primeiro dia; duração da posição dentro da janela pode ser menor.
- Encerrar antes se esta operação fechar; não forçar liquidação no fim.
- Trader e D1/H4: respostas fonte gravadas, respeitando os horários originais
  de disponibilidade; fonte `../brooks_walkforward_deepseek_v4_1_flash_corrected_2026-09-20_2026-09-29/`.
- PM: novas inferências reais via PydanticAI/API OpenCode Go,
  `custom@opencode-go:deepseek-v4.1-flash`. Nenhum CLI de inferência.
- PM periódico 30min históricos; eventos de abertura, ordens, fills e
  vencimento de duração também podem acordá-lo. Timeout explícito 180s por
  role, igual ao estágio 5R anterior. Nenhuma regra de retry foi alterada.
- Execution venue/clock simulados; não há envio de ordem real Binance.
  Equity inicial 10000 USDT, fee 0,0004, slippage adverso 1bps, sem funding.
- Exceção de LONG explicitamente limitada por
  `management_policy.applicable_risk_behavior.operation_correlation_id`,
  igual ao correlation ID atual. R original congelado; limite estrutural
  informa contexto e unlock, e não força stop/trava em 1R.
- Perda líquida conjunta projetada de 5R em M1 fechado solicita trava
  completa pelo GM existente. PM pode antecipar hedge por evidência.
- MAIN não fecha/reduz com líquido conjunto projetado negativo ou desconhecido,
  e não fecha deixando hedge órfã. HEDGE pode realizar prejuízo em unwind
  fundamentado mantendo MAIN aberta. Estado fresco é exigido para MAIN exit.
- Duração: TIME_LIMIT vira wake; HOLD aprovado após vencer prolonga a
  avaliação em 30min. O horizonte máximo continua finito.
- **TP permanece fixo na abertura.** O contrato PM atual não permite
  deslocar/remover TP. Não se introduziu gestão dinâmica de alvo neste teste.

## Perguntas do teste

O PM vai realizar/reduzir no período de líquido positivo, antecipar hedge
quando a estrutura se deteriorar, ou sustentar exposição? Como vai responder
ao vencimento das 24h? Se nada encerrar, qual é o líquido marcado final e
qual custo/exposição continua em aberto?

A seleção usou trajetória posterior para encontrar um cenário de gestão
mais exigente; esses fatos não entram antecipadamente nos packets. Não é
um teste de edge nem julgamento ex-ante da decisão de entrada.

## Verificação antes da execução

```sh
.venv/bin/pytest -q tests/test_brooks_first_long_replay.py tests/test_brooks_long_lock_venue.py tests/test_brooks_pm.py tests/test_brooks_prompt_runtime.py
```

**26 passed** (4,63s), 12 avisos Pydantic já existentes sobre `schema`.
A integração usa os M1 históricos para provar a ativação do ID selecionado,
opt-in coerente com a identidade, R fixo e wake de duração sem STOP/TIME_LIMIT
negativo automático. Nenhuma alteração em GM/ExecutionPort/PM consumer.

Todos os prompts literais, respostas brutas, tools, wire requests,
resultados do GM e checkpoints serão preservados nesta pasta.

## Interrupção às 24h e correção necessária

O processo inicial terminou no primeiro wake de duração com `KeyError: payload`.
O produtor publicou um PM_TIMER completo, mas enfileirou separadamente um
dict sem payload/event_id. A desserialização estrita impediu o dispatch antes
de qualquer inferência PM nesse wake. As 50 avaliações anteriores foram
aceitas, todas HOLD; não houve write após o último checkpoint.

Evidência original, log e checkpoint preservados em `interruption_24h/`.
A correção faz publicar e enfileirar o MESMO BrooksEvent completo.
O teste agora desserializa o evento real e passa HOLD tipado por PM/GM reais,
confirmando extensão do prazo e nenhuma ordem/fill/entrada duplicada.
O mesmo comando de verificação passou novamente: **26 passed**.

A retomada usa cópia independente do último checkpoint (22/09 15:30:29.898 UTC),
preserva os 50 role runs/wire requests e remove apenas o sufixo não
checkpointado do journal/prova de duração na cópia de trabalho. O sufixo
original permanece no arquivo de interrupção. A troca de identidade de
código será documentada em RESUME_LINEAGE.json, sem relaxar o gate de restart.
A pausa real de manutenção não avança o relógio histórico. Não há nova
entrada ou repetição das 50 decisões. O packet do wake vencido é capturado
na sua hora histórica, sem candles posteriores ou alteração da política.
